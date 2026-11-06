"""Проверяет парные прогоны, решение оценщика и запрет переноса при отказе.

Вход: временные сценарии и ответы процессов. Выход: unittest без сетевых запросов.
Класс ReplayEvaluationTests: setUp, tearDown — файлы; prepare, review, worker,
judge, dialogue — имитация границ; execute — полный порядок; test_candidate_publishes,
test_current_preserves_memory, test_inconclusive_preserves_memory,
test_failed_worker_preserves_memory, test_invalid_judge_preserves_memory,
test_disabled_flag_does_not_run, test_review_rejection_does_not_run,
test_replay_memory_change_is_not_published, test_reference_is_not_in_workspace,
test_loader_rejects_overlapping_inputs, test_loader_requires_reference_answer,
проверки отсутствующих данных — сценарии принятия и отказа.
check_replay_data проверяется отсутствующим сценарием, отсутствием реплик и ошибкой чтения.
Класс ReplayMetricsTests проверяет callbacks и токены.
test_evaluation_packet_preserves_answers_and_marks_omitted_files проверяет
полноту основных материалов и явное обозначение пропущенных приложений.
"""
import importlib.util
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import command_agent_memory.memory_replay.replay_evaluation as replay
import command_agent_memory.memory_replay.replay_scenario as scenarios
import command_agent_memory.memory_update.personal_memory_update as pipeline
from command_agent_memory.memory_update.memory_proposal import MemoryProposal
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.skill_resources import ROOT


class ReplayEvaluationTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет решение о переносе и сохранность входов."""

    def setUp(self):
        """Без входов создаёт память, исходные данные и прежний ответ; возвращает None."""
        temporary_root = ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.target, self.run = self.root / "user", self.root / "run"
        self.before = {"AGENTS.md": b"old"}
        self.after = {"AGENTS.md": b"new", "knowledge/INDEX.md": b"index"}
        write_snapshot(self.target, self.before)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        (self.inputs / "input.txt").write_bytes(b"original input")
        self.reference = self.root / "reference"
        self.reference.mkdir()
        (self.reference / "answer.md").write_bytes(b"old answer")
        self.scenario_path = self.root / "scenario.json"
        self.scenario_path.write_text(json.dumps({
            "agent": {"factory": "worker_fixture:create_session"},
            "request": "Реши задачу и запиши output.txt", "messages": ["Реши задачу и запиши output.txt"], "inputs": "inputs", "reference": "reference",
        }), encoding="utf-8")
        self.scenario = scenarios.load_replay_scenario(self.scenario_path)
        self.choice = "candidate"
        self.worker_calls = 0
        self.judge_calls = 0
        self.worker_return_code = 0
        self.invalid_judge = False
        self.reject_review = False
        self.mutate_memory = False
        self.reviewed_memory = None
        self.baseline_state = "completed"

    def tearDown(self):
        """Без входов освобождает временную область; возвращает None."""
        self.assertTrue(self.root.resolve().is_relative_to((ROOT / "tmp").resolve()))
        self.temporary.cleanup()

    def test_replay_decision_with_prefix_is_parsed(self):
        """Передаёт пояснение и JSON; возвращает проверенное решение candidate."""
        reply = 'Проверка завершена.\n```json\n{"choice":"candidate","summary":"Новый результат лучше"}\n```'
        self.assertEqual(
            replay.parse_replay_decision(reply),
            {"choice": "candidate", "summary": "Новый результат лучше"},
        )

    def test_evaluation_packet_preserves_answers_and_marks_omitted_files(self):
        """Без входов собирает ответы и приложения; проверяет точность и обозначение пропусков."""
        result = self.root / "result"
        result.mkdir()
        (result / "answer.md").write_text("Новый ответ: 97", encoding="utf-8")
        (result / "metrics.json").write_text('{"user_turns":1,"usage":null}', encoding="utf-8")
        (self.inputs / "large.txt").write_text("x" * 3000, encoding="utf-8")
        (self.inputs / "binary.bin").write_bytes(b"\xff\x00")
        packet = json.loads(replay.build_evaluation_packet(self.root, self.scenario, text_limit=2000))
        documents = {item["path"]: item for item in packet["documents"]}
        self.assertEqual(packet["messages"], list(self.scenario.messages))
        self.assertEqual(documents[(self.reference / "answer.md").as_posix()]["text"], "old answer")
        self.assertEqual(documents[(result / "answer.md").as_posix()]["text"], "Новый ответ: 97")
        self.assertIsNone(json.loads(documents[(result / "metrics.json").as_posix()]["text"])["usage"])
        self.assertEqual(documents[(self.inputs / "input.txt").as_posix()]["text"], "original input")
        for name in ("large.txt", "binary.bin"):
            item = documents[(self.inputs / name).as_posix()]
            self.assertIn("omitted", item)
            self.assertNotIn("text", item)

    def prepare(self, target, stage, materials, instructions_path, executable, model, run, knowledge_materials):
        """Принимает параметры подготовки; создаёт предложение без LLM, возвращает MemoryProposal."""
        write_snapshot(run / "before", self.before)
        write_snapshot(run / "before-review", self.after)
        return MemoryProposal(target, stage, run, self.before, self.after)

    def review(self, proposal, executable, model):
        """Принимает предложение и модель; возвращает итоговую копию и решение ревью."""
        if self.reviewed_memory is not None:
            self.after = self.reviewed_memory
        write_snapshot(proposal.run / "after", self.after)
        proposal = MemoryProposal(
            proposal.target, proposal.stage, proposal.run, proposal.before, self.after,
        )
        if self.reject_review:
            return proposal, {"decision": "rejected", "reviews": 1, "review": "review.json", "summary": "Отказ"}
        return proposal, {"decision": "accepted", "reviews": 1}

    def worker(self, command, **kwargs):
        """Принимает команду процесса; создаёт результат в копии, возвращает CompletedProcess."""
        self.worker_calls += 1
        workspace = Path(command[command.index("--workspace") + 1])
        result = Path(command[command.index("--result") + 1])
        self.assertFalse((workspace / "reference").exists())
        self.assertEqual(read_memory(workspace, "personal"), self.before if workspace.parent.name == "baseline" else self.after)
        self.assertEqual((workspace / "inputs/input.txt").read_bytes(), b"original input")
        result.mkdir()
        (result / "answer.md").write_bytes(b"new answer")
        (result / "metrics.json").write_text(json.dumps({
            "state": self.baseline_state if workspace.parent.name == "baseline" else "completed",
            "duration_seconds": 1, "model_calls": 2,
            "tool_calls": 1, "usage": None, "usage_complete": False,
        }), encoding="utf-8")
        (result / "transcript.jsonl").write_text("", encoding="utf-8")
        (workspace / "output.txt").write_bytes(b"result")
        if self.mutate_memory:
            (workspace / "AGENTS.md").write_bytes(b"changed by agent")
        return subprocess.CompletedProcess(command, self.worker_return_code)

    def judge(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает параметры оценщика; возвращает заданное решение без LLM."""
        self.judge_calls += 1
        self.assertFalse(writable)
        self.assertTrue((self.run / "replay/result/files/output.txt").is_file())
        return "не JSON" if self.invalid_judge else json.dumps({"choice": self.choice, "summary": "Причина выбора"})


    def dialogue(self, workspace, scenario, result, model, **kwargs):
        """Принимает входы диалога; имитирует завершённую задачу либо сбой, возвращает None."""
        outcome = self.worker(["--workspace", str(workspace), "--result", str(result)])
        if outcome.returncode:
            raise RuntimeError("Прогон не завершился")

    def execute(self, enabled=True, scenario_missing=False):
        """Принимает флаг и наличие сценария; выполняет целевой личный цикл без моделей."""
        skill = ROOT / "command_agent_memory/skills/personal-memory-consolidation/SKILL.md"
        with patch.object(pipeline, "prepare_memory", side_effect=self.prepare):
            with patch.object(pipeline, "review_memory", side_effect=self.review):
                with patch("command_agent_memory.memory_replay.dialogue_runner.run_dialogue", side_effect=self.dialogue):
                    with patch.object(replay, "run_codex", side_effect=self.judge):
                        pipeline.consolidate_with_review(
                            self.target, "personal", "Сведения", skill, "codex", "test", self.run,
                            replay_scenario=self.scenario if enabled and not scenario_missing else None,
                            user_id="101", replay_requested=enabled,
                        )

    def test_candidate_publishes(self):
        """Без входов проверяет перенос после двух прогонов и выбора candidate; возвращает None."""
        self.execute()
        self.assertEqual(self.worker_calls, 2)
        self.assertEqual(self.judge_calls, 1)
        self.assertEqual(read_memory(self.target, "personal"), self.after)
        self.assertEqual(json.loads((self.run / "status.json").read_text())["replay_choice"], "candidate")

    @unittest.skipUnless(importlib.util.find_spec("deepagents"), "Нужно окружение DeepAgents")
    def test_dialogue_backend_uses_messages_and_saved_candidate(self):
        """Запускает диалоговую границу с имитацией; проверяет отдельные реплики и итоговую оценку."""
        self.run.mkdir()
        proposal = MemoryProposal(self.target, "personal", self.run, self.before, self.after)
        scenario = scenarios.ReplayScenario("Первая реплика", self.inputs, self.reference,
                                         messages=("Первая реплика", "Уточнение"), goal="Получить результат", agent={"factory": "worker_fixture:create_session"})

        def dialogue(workspace, passed_scenario, result, model, **kwargs):
            """Принимает подготовленную копию; создаёт наблюдаемый результат диалога без модели."""
            self.assertEqual(passed_scenario.messages, scenario.messages)
            self.assertEqual(passed_scenario.request, "Первая реплика")
            self.assertEqual(kwargs["worker_session"].profile["model"], "test-model")
            self.worker(["--workspace", str(workspace), "--result", str(result)])

        with patch("command_agent_memory.memory_replay.dialogue_runner.run_dialogue", side_effect=dialogue):
            decision = replay.evaluate_by_replay(proposal, scenario, "", "test-model",
                                                 "101", runner=self.judge)
        self.assertEqual(decision["choice"], "candidate")
        self.assertEqual(self.worker_calls, 2)
        self.assertEqual(self.judge_calls, 1)


    def test_current_preserves_memory(self):
        """Без входов проверяет отсутствие переноса при выборе current; возвращает None."""
        self.choice = "current"
        with self.assertRaisesRegex(RuntimeError, "после прогона"):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        self.assertEqual(self.worker_calls, 2)

    def test_pair_preserves_before_after_and_records_common_profile(self):
        """Проверяет разные снимки памяти, одинаковые входы и сохранённый профиль парного опыта."""
        self.execute()
        directory = self.run / "replay"
        self.assertEqual(read_memory(directory / "baseline/workspace", "personal"), self.before)
        self.assertEqual(read_memory(directory / "workspace", "personal"), self.after)
        comparison = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
        self.assertIn("input.txt", comparison["inputs_sha256"])
        self.assertEqual(len(comparison["agent_profile_sha256"]), 64)
        self.assertTrue((directory / "baseline/result/files/output.txt").is_file())

    def test_missing_agent_blocks_publication_without_fallback(self):
        """Убирает подключение рабочего агента; проверяет отказ до выполнения задачи."""
        self.scenario = replace(self.scenario, agent=None)
        with self.assertRaisesRegex(ValueError, "agent"):
            self.execute()
        self.assertEqual((self.worker_calls, self.judge_calls), (0, 0))
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_mismatched_profiles_close_both_sessions_before_execution(self):
        """Меняет профиль инструментов второй сессии; проверяет cleanup и отказ до двух прогонов."""
        from command_agent_memory.worker_agents import WorkerSession
        from worker_fixture import profile

        first, second = profile(), profile()
        second["tools"][0]["schema"] = {"type": "string"}
        cleanups = [Mock(), Mock()]
        sessions = [WorkerSession(Mock(), first, cleanups[0]), WorkerSession(Mock(), second, cleanups[1])]
        with patch.object(replay, "load_worker_session", side_effect=sessions):
            with self.assertRaisesRegex(RuntimeError, "Профили"):
                self.execute()
        self.assertEqual((self.worker_calls, self.judge_calls), (0, 0))
        for cleanup in cleanups:
            cleanup.assert_called_once_with()
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_changed_inputs_block_comparison(self):
        """Изменяет вход второй копии; проверяет отказ от несопоставимого опыта."""
        original = replay.prepare_workspace

        def change_inputs(proposal, scenario, destination, memory):
            """Принимает копию; после подготовки candidate изменяет один входной файл."""
            original(proposal, scenario, destination, memory)
            if destination.parent.name == "replay":
                (destination / "inputs/input.txt").write_bytes(b"different")

        with patch.object(replay, "prepare_workspace", side_effect=change_inputs):
            with self.assertRaisesRegex(RuntimeError, "Исходные данные"):
                self.execute()
        self.assertEqual((self.worker_calls, self.judge_calls), (0, 0))

    def test_incomplete_baseline_can_demonstrate_candidate_quality_gain(self):
        """Оставляет цель baseline открытой; проверяет возможность оценить завершённый candidate."""
        self.baseline_state = "inconclusive"
        self.execute()
        self.assertEqual((self.worker_calls, self.judge_calls), (2, 1))
        self.assertEqual(read_memory(self.target, "personal"), self.after)

    def test_reviewed_memory_reaches_replay(self):
        """Без входов проверяет прогон и перенос исправленной ревьюером версии; возвращает None."""
        self.reviewed_memory = {"AGENTS.md": b"reviewer correction", "knowledge/INDEX.md": b"reviewed index"}
        self.execute()
        self.assertEqual(read_memory(self.run / "replay/workspace", "personal"), self.reviewed_memory)
        self.assertEqual(read_memory(self.target, "personal"), self.reviewed_memory)
        self.assertNotEqual(read_memory(self.run / "before-review", "personal"), self.reviewed_memory)

    def test_inconclusive_preserves_memory(self):
        """Без входов проверяет отсутствие переноса при inconclusive; возвращает None."""
        self.choice = "inconclusive"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_failed_worker_preserves_memory(self):
        """Без входов проверяет отказ при ошибке процесса без вызова оценщика; возвращает None."""
        self.worker_return_code = 1
        with self.assertRaisesRegex(RuntimeError, "Прогон не завершился"):
            self.execute()
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_invalid_judge_preserves_memory(self):
        """Без входов проверяет сохранность при повреждённом решении оценщика; возвращает None."""
        self.invalid_judge = True
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_disabled_flag_does_not_run(self):
        """Без входов проверяет прежний путь по ревью без рабочего прогона; возвращает None."""
        self.execute(enabled=False)
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(read_memory(self.target, "personal"), self.after)

    def test_review_rejection_does_not_run(self):
        """Без входов проверяет отсутствие прогона после отклонённого ревью; возвращает None."""
        self.reject_review = True
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_replay_memory_change_is_not_published(self):
        """Без входов проверяет отказ при изменении памяти решающим агентом; возвращает None."""
        self.mutate_memory = True
        with self.assertRaisesRegex(RuntimeError, "изменил память"):
            self.execute()
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_reference_is_not_in_workspace(self):
        """Без входов проверяет раздельное хранение ответов и сохранность исходных файлов; возвращает None."""
        self.execute()
        self.assertFalse((self.run / "replay/workspace/reference").exists())
        self.assertEqual((self.inputs / "input.txt").read_bytes(), b"original input")
        self.assertEqual((self.reference / "answer.md").read_bytes(), b"old answer")
        self.assertEqual((self.run / "replay/reference/answer.md").read_bytes(), b"old answer")

    def test_loader_rejects_overlapping_inputs(self):
        """Без входов проверяет запрет копировать прежний ответ среди входов; возвращает None."""
        self.scenario_path.write_text(json.dumps({
            "request": "Запрос", "inputs": ".", "reference": "reference",
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "раздельно"):
            scenarios.load_replay_scenario(self.scenario_path)

    def test_loader_requires_reference_answer(self):
        """Без входов проверяет обязательный прежний ответ до запуска моделей; возвращает None."""
        scenario = self.root / "missing.json"
        scenario.write_text(json.dumps({"request": "Запрос", "inputs": "reference", "reference": "inputs"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "answer.md"):
            scenarios.load_replay_scenario(scenario)


    def test_missing_scenario_is_accepted_by_review(self):
        """Проверяет принятие по review без данных; возвращает None."""
        self.execute(scenario_missing=True)
        self.assertEqual((self.worker_calls, self.judge_calls), (0, 0))
        status = json.loads((self.run / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["replay_status"], "skipped")
        self.assertNotIn("replay_choice", status)
        self.assertEqual(read_memory(self.target, "personal"), self.after)

    def test_request_without_dialogue_messages_skips(self):
        """Проверяет, что одношаговый сценарий не запускается как диалог; возвращает None."""
        self.scenario = scenarios.ReplayScenario("Запрос", self.inputs, self.reference)
        self.execute()
        self.assertEqual(self.worker_calls, 0)
        assessment = json.loads((self.run / "replay-assessment/decision.json").read_text(encoding="utf-8"))
        self.assertEqual(assessment["decision"], "skip")
        self.assertTrue(assessment["missing"])

    def test_data_check_error_does_not_publish(self):
        """Проверяет остановку при ошибке чтения данных вместо пропуска; возвращает None."""
        with patch.object(scenarios, "check_replay_data", side_effect=OSError("Ошибка чтения")):
            with self.assertRaisesRegex(OSError, "Ошибка чтения"):
                self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)

    def test_incomplete_context_is_available_to_assessor(self):
        """Без входов проверяет загрузку отсутствующих данных для оценки LLM; возвращает None."""
        self.scenario_path.write_text(json.dumps({
            "inputs": "missing-inputs", "reference_metrics": {"duration_seconds": 18000},
            "resource_notes": "Большая выгрузка из базы",
        }), encoding="utf-8")
        scenario = scenarios.load_replay_scenario(self.scenario_path, allow_incomplete=True)
        self.assertEqual(scenario.request, "")
        self.assertIsNone(scenario.reference)
        self.assertEqual(scenario.reference_metrics["duration_seconds"], 18000)
        self.assertFalse(scenario.inputs.exists())


@unittest.skipUnless(importlib.util.find_spec("langchain_core"), "Нужно окружение с LangChain")
class ReplayMetricsTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет счётчики и суммирование сообщённого usage."""

    def test_missing_callbacks_do_not_become_zero_cost(self):
        """Проверяет неизвестные счётчики внешнего агента при отсутствии callbacks."""
        from command_agent_memory.memory_replay.dialogue_runner import ReplayMetrics, metrics_record

        result = metrics_record(ReplayMetrics(), 1.2, require_observation=True)
        self.assertIsNone(result["model_calls"])
        self.assertIsNone(result["tool_calls"])
        self.assertIsNone(result["usage"])

    def test_nested_usage_and_calls(self):
        """Без входов проверяет два запроса модели и вызов инструмента; возвращает None."""
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult
        from command_agent_memory.memory_replay.dialogue_runner import ReplayMetrics

        callback = ReplayMetrics()
        response = LLMResult(generations=[[ChatGeneration(message=AIMessage(
            content="Ответ", response_metadata={"model_name": "test"},
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                            "input_token_details": {"cache_read": 4}},
        ))]])
        for _ in range(2):
            callback.on_chat_model_start({}, [])
            callback.on_llm_end(response)
        callback.on_tool_start({}, "input")
        self.assertEqual(callback.model_calls, 2)
        self.assertEqual(callback.tool_calls, 1)
        self.assertEqual(callback.usage_calls, 2)
        self.assertEqual(callback.usage_metadata["test"]["input_tokens"], 20)
        self.assertEqual(callback.usage_metadata["test"]["input_token_details"]["cache_read"], 8)


if __name__ == "__main__":
    unittest.main()
