"""Проверяет один прогон, решение оценщика и запрет переноса при отказе.

Вход: временные сценарии и ответы процессов. Выход: unittest без сетевых запросов.
Класс ReplayEvaluationTests: setUp, tearDown — файлы; prepare, review, worker,
judge — имитация границ; execute — полный порядок; test_candidate_publishes,
test_current_preserves_memory, test_inconclusive_preserves_memory,
test_failed_worker_preserves_memory, test_invalid_judge_preserves_memory,
test_disabled_flag_does_not_run, test_review_rejection_does_not_run,
test_replay_memory_change_is_not_published, test_reference_is_not_in_workspace,
test_loader_rejects_overlapping_inputs, test_loader_requires_reference_answer,
test_cli_accepts_missing_scenario — сценарии принятия и отказа.
assess — имитация оценки возможности; test_skip_publishes_without_worker,
test_missing_scenario_can_skip, test_assessment_failure_preserves_memory,
test_incomplete_context_is_available_to_assessor, test_assessor_reads_saved_evidence,
test_invalid_assessment_protocol, test_skip_preserves_expensive_measurements — оценка и пропуск.
test_run_without_scenario_is_not_published — отказ при разрешении без сценария.
test_structured_assessment_is_reported — объяснения JSON без жёсткого шаблона текста.
test_reviewed_memory_reaches_replay — прогон после исправлений ревьюера.
Класс ReplayMetricsTests: test_nested_usage_and_calls — callbacks и токены.
test_force_replay_bypasses_assessment — обязательный прогон без оценки пропуска.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import replay_evaluation as replay
import reviewed_consolidation as pipeline


class ReplayEvaluationTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет решение о переносе и сохранность входов."""

    def setUp(self):
        """Без входов создаёт память, исходные данные и прежний ответ; возвращает None."""
        temporary_root = pipeline.ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.target, self.run = self.root / "user", self.root / "run"
        self.before = {"AGENTS.md": b"old"}
        self.after = {"AGENTS.md": b"new", "knowledge/INDEX.md": b"index"}
        pipeline.write_snapshot(self.target, self.before)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        (self.inputs / "input.txt").write_bytes(b"original input")
        self.reference = self.root / "reference"
        self.reference.mkdir()
        (self.reference / "answer.md").write_bytes(b"old answer")
        self.scenario_path = self.root / "scenario.json"
        self.scenario_path.write_text(json.dumps({
            "request": "Реши задачу и запиши output.txt", "inputs": "inputs", "reference": "reference",
        }), encoding="utf-8")
        self.scenario = replay.load_replay_scenario(self.scenario_path)
        self.choice = "candidate"
        self.worker_calls = 0
        self.judge_calls = 0
        self.worker_return_code = 0
        self.invalid_judge = False
        self.reject_review = False
        self.mutate_memory = False
        self.reviewed_memory = None
        self.assessment_choice = "run"
        self.assessment_error = False
        self.assessment_calls = 0
        self.structured_assessment = False

    def tearDown(self):
        """Без входов освобождает временную область; возвращает None."""
        self.assertTrue(self.root.resolve().is_relative_to((pipeline.ROOT / "tmp").resolve()))
        self.temporary.cleanup()

    def prepare(self, target, stage, materials, skill_path, executable, model, run, knowledge_materials):
        """Принимает параметры подготовки; создаёт предложение без LLM, возвращает MemoryProposal."""
        pipeline.write_snapshot(run / "before", self.before)
        pipeline.write_snapshot(run / "before-review", self.after)
        return pipeline.MemoryProposal(target, stage, run, self.before, self.after)

    def review(self, proposal, executable, model):
        """Принимает предложение и модель; возвращает итоговую копию и решение ревью."""
        if self.reviewed_memory is not None:
            self.after = self.reviewed_memory
        pipeline.write_snapshot(proposal.run / "after", self.after)
        proposal = pipeline.MemoryProposal(
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
        self.assertEqual(pipeline.read_memory(workspace, "personal"), self.after)
        self.assertEqual((workspace / "inputs/input.txt").read_bytes(), b"original input")
        result.mkdir()
        (result / "answer.md").write_bytes(b"new answer")
        (result / "metrics.json").write_text(json.dumps({
            "state": "completed", "duration_seconds": 1, "model_calls": 2,
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

    def assess(self, proposal, scenario, executable, model, context, agent_python):
        """Принимает входы оценки; возвращает заданное run/skip либо вызывает сбой без выполнения задачи."""
        self.assessment_calls += 1
        if self.assessment_error:
            raise RuntimeError("Сбой оценки возможности")
        return {"decision": self.assessment_choice, "summary": "Причина решения о прогоне",
                "context": {"missing": ["Снимок базы"]} if self.structured_assessment else "Сведения о контексте",
                "resources": {"duration_seconds": 18000} if self.structured_assessment else "Сведения о затратах"}

    def execute(self, enabled=True, scenario_missing=False, force_replay=False):
        """Принимает признак прогона; выполняет порядок с имитацией процессов, возвращает None."""
        skill = pipeline.ROOT / "agents/consolidation/skills/consolidation/SKILL.md"
        with patch.object(pipeline, "prepare_memory", side_effect=self.prepare):
            with patch.object(pipeline, "review_memory", side_effect=self.review):
                with patch.object(replay.subprocess, "run", side_effect=self.worker):
                    with patch.object(replay, "run_codex", side_effect=self.judge), patch.object(replay, "assess_replay", side_effect=self.assess):
                        pipeline.consolidate_with_review(
                            self.target, "personal", "Сведения", skill, "codex", "test", self.run,
                            replay_scenario=self.scenario if enabled and not scenario_missing else None,
                            agent_python=Path(sys.executable), user_id="101",
                            replay_requested=enabled,
                            force_replay=force_replay,
                        )

    def test_candidate_publishes(self):
        """Без входов проверяет перенос только после одного прогона и выбора candidate; возвращает None."""
        self.execute()
        self.assertEqual(self.worker_calls, 1)
        self.assertEqual(self.judge_calls, 1)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.after)
        self.assertEqual(json.loads((self.run / "status.json").read_text())["replay_choice"], "candidate")

    def test_force_replay_bypasses_assessment(self):
        """Без входов проверяет обязательный прогон без LLM-оценки пропуска; возвращает None."""
        self.execute(force_replay=True)
        self.assertEqual(self.assessment_calls, 0)
        self.assertEqual(self.worker_calls, 1)
        self.assertEqual(self.judge_calls, 1)

    def test_current_preserves_memory(self):
        """Без входов проверяет отсутствие переноса при выборе current; возвращает None."""
        self.choice = "current"
        with self.assertRaisesRegex(RuntimeError, "после прогона"):
            self.execute()
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)
        self.assertEqual(self.worker_calls, 1)

    def test_reviewed_memory_reaches_replay(self):
        """Без входов проверяет прогон и перенос исправленной ревьюером версии; возвращает None."""
        self.reviewed_memory = {"AGENTS.md": b"reviewer correction", "knowledge/INDEX.md": b"reviewed index"}
        self.execute()
        self.assertEqual(pipeline.read_memory(self.run / "replay/workspace", "personal"), self.reviewed_memory)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.reviewed_memory)
        self.assertNotEqual(pipeline.read_memory(self.run / "before-review", "personal"), self.reviewed_memory)

    def test_inconclusive_preserves_memory(self):
        """Без входов проверяет отсутствие переноса при inconclusive; возвращает None."""
        self.choice = "inconclusive"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_failed_worker_preserves_memory(self):
        """Без входов проверяет отказ при ошибке процесса без вызова оценщика; возвращает None."""
        self.worker_return_code = 1
        with self.assertRaisesRegex(RuntimeError, "Прогон не завершился"):
            self.execute()
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_invalid_judge_preserves_memory(self):
        """Без входов проверяет сохранность при повреждённом решении оценщика; возвращает None."""
        self.invalid_judge = True
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_disabled_flag_does_not_run(self):
        """Без входов проверяет прежний путь по ревью без рабочего прогона; возвращает None."""
        self.execute(enabled=False)
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.after)

    def test_review_rejection_does_not_run(self):
        """Без входов проверяет отсутствие прогона после отклонённого ревью; возвращает None."""
        self.reject_review = True
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_replay_memory_change_is_not_published(self):
        """Без входов проверяет отказ при изменении памяти решающим агентом; возвращает None."""
        self.mutate_memory = True
        with self.assertRaisesRegex(RuntimeError, "изменил память"):
            self.execute()
        self.assertEqual(self.judge_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

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
            replay.load_replay_scenario(self.scenario_path)

    def test_loader_requires_reference_answer(self):
        """Без входов проверяет обязательный прежний ответ до запуска моделей; возвращает None."""
        scenario = self.root / "missing.json"
        scenario.write_text(json.dumps({"request": "Запрос", "inputs": "reference", "reference": "inputs"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "answer.md"):
            replay.load_replay_scenario(scenario)

    def test_cli_accepts_missing_scenario(self):
        """Без входов проверяет разрешение оценки без сценария, без запуска моделей; возвращает None."""
        reports = self.target / "raw/chat-reports-codex/session-one"
        reports.mkdir(parents=True)
        (reports / "context.md").write_text("Контекст", encoding="utf-8")
        (reports / "procedures.md").write_text("Опыт", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(pipeline.ROOT / "consolidate_reports_codex.py"),
             "--user-id", "101", "--workspace", str(self.target), "--evaluate-by-replay", "--validate-only"],
            cwd=pipeline.ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("возможен пропуск", result.stdout)

    def test_skip_publishes_without_worker(self):
        """Без входов проверяет перенос по ревью при skip и явное отсутствие успешного прогона; возвращает None."""
        self.assessment_choice = "skip"
        self.execute()
        self.assertEqual(self.assessment_calls, 1)
        self.assertEqual((self.worker_calls, self.judge_calls), (0, 0))
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.after)
        status = json.loads((self.run / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["replay_status"], "skipped")
        self.assertNotIn("replay_choice", status)
        self.assertFalse((self.run / "replay").exists())
        self.assertIn("Прогон не выполнен", (self.run / "report/replay.md").read_text(encoding="utf-8"))

    def test_missing_scenario_can_skip(self):
        """Без входов проверяет оценку и пропуск при отсутствии сценария; возвращает None."""
        self.assessment_choice = "skip"
        self.execute(scenario_missing=True)
        self.assertEqual(self.assessment_calls, 1)
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.after)

    def test_structured_assessment_is_reported(self):
        """Без входов проверяет сохранение структуры объяснения без падения и запуска worker; возвращает None."""
        self.structured_assessment = True
        self.assessment_choice = "skip"
        self.execute()
        text = (self.run / "report/replay-assessment.md").read_text(encoding="utf-8")
        self.assertIn("Снимок базы", text)
        self.assertIn("18000", text)
        self.assertEqual(self.worker_calls, 0)

    def test_run_without_scenario_is_not_published(self):
        """Без входов проверяет отсутствие переноса при ошибочном разрешении без сценария; возвращает None."""
        with self.assertRaisesRegex(RuntimeError, "без предоставленного сценария"):
            self.execute(scenario_missing=True)
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_assessment_failure_preserves_memory(self):
        """Без входов проверяет запрет переноса при сбое оценки вместо её пропуска; возвращает None."""
        self.assessment_error = True
        with self.assertRaisesRegex(RuntimeError, "Сбой оценки"):
            self.execute()
        self.assertEqual(self.worker_calls, 0)
        self.assertEqual(pipeline.read_memory(self.target, "personal"), self.before)

    def test_incomplete_context_is_available_to_assessor(self):
        """Без входов проверяет загрузку отсутствующих данных для оценки LLM; возвращает None."""
        self.scenario_path.write_text(json.dumps({
            "inputs": "missing-inputs", "reference_metrics": {"duration_seconds": 18000},
            "resource_notes": "Большая выгрузка из базы",
        }), encoding="utf-8")
        scenario = replay.load_replay_scenario(self.scenario_path, allow_incomplete=True)
        self.assertEqual(scenario.request, "")
        self.assertIsNone(scenario.reference)
        self.assertEqual(scenario.reference_metrics["duration_seconds"], 18000)
        self.assertFalse(scenario.inputs.exists())

    def test_assessor_reads_saved_evidence(self):
        """Без входов проверяет передачу измерений read-only оценщику и сохранение решения; возвращает None."""
        self.run.mkdir()
        proposal = pipeline.MemoryProposal(self.target, "personal", self.run, self.before, self.after)
        with patch.object(replay, "run_codex", return_value=json.dumps({
            "decision": "skip", "summary": "Выполнение длится часы",
            "context": {"missing": ["Снимок базы"]}, "resources": {"estimate": "Длительный запрос"},
        })) as assess:
            result = replay.assess_replay(proposal, self.scenario, "codex", "test", self.root)
        self.assertEqual(result["decision"], "skip")
        self.assertEqual(result["context"]["missing"], ["Снимок базы"])
        self.assertNotIn("writable", assess.call_args.kwargs)
        information = json.loads((self.run / "replay-assessment/scenario.json").read_text(encoding="utf-8"))
        self.assertEqual(information["request"], self.scenario.request)
        self.assertEqual(information["reference"], str(self.reference))
        self.assertEqual(information["materials"], str(self.run / "materials.md"))
        self.assertTrue((self.run / "replay-assessment/decision.json").exists())

    def test_invalid_assessment_protocol(self):
        """Без входов проверяет отказ от некорректного решения оценки, не превращая его в skip; возвращает None."""
        self.run.mkdir()
        proposal = pipeline.MemoryProposal(self.target, "personal", self.run, self.before, self.after)
        with patch.object(replay, "run_codex", return_value='{"decision":"skip"}'):
            with self.assertRaisesRegex(RuntimeError, "протокол оценки"):
                replay.assess_replay(proposal, None, "codex", "test")

    def test_skip_preserves_expensive_measurements(self):
        """Без входов проверяет передачу времени и объёма исходной задачи без запуска worker; возвращает None."""
        self.run.mkdir()
        scenario = replay.ReplayScenario("Большой запрос", self.inputs, self.reference,
                                         {"duration_seconds": 18000, "result_bytes": 40_000_000_000})
        proposal = pipeline.MemoryProposal(self.target, "personal", self.run, self.before, self.after)
        with patch.object(replay, "run_codex", return_value=json.dumps({
            "decision": "skip", "summary": "Повтор требует нескольких часов",
            "context": "Контекст доступен", "resources": "5 часов, 40 ГБ",
        })), patch.object(replay.subprocess, "run") as worker:
            replay.assess_replay(proposal, scenario, "codex", "test")
        worker.assert_not_called()
        recorded = json.loads((self.run / "replay-assessment/scenario.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["reference_metrics"], scenario.reference_metrics)


@unittest.skipUnless(importlib.util.find_spec("langchain_core"), "Нужно окружение с LangChain")
class ReplayMetricsTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет счётчики и суммирование сообщённого usage."""

    def test_nested_usage_and_calls(self):
        """Без входов проверяет два запроса модели и вызов инструмента; возвращает None."""
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult
        from replay_worker import ReplayMetrics

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
