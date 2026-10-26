"""Проверяет диалог двух агентов на локальных данных без обращения к моделям.

Классы: DialogueScenarioTests — загрузка отдельных реплик;
ScriptedSubject — имитация тестируемого агента со stream;
DialogueReplayTests — изоляция, порядок реплик, завершение, сбои и сборка агента.
Методы setUp/tearDown готовят и освобождают данные; execute запускает имитацию;
test_* проверяют наблюдаемое поведение, parse — строгие ответы тестировщика.
"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_pipeline import ROOT
from replay_evaluation import ReplayScenario, load_replay_scenario
from run_dialogue_replay import prepare_workspace


HAS_RUNTIME = bool(importlib.util.find_spec("langchain_core") and importlib.util.find_spec("deepagents"))


class DialogueScenarioTests(unittest.TestCase):
    """Проверяет загрузку исходного диалога и чистые каталоги без модели."""

    def test_first_message_is_exact_source(self):
        """Загружает сценарий Анны; подтверждает четыре отдельные исходные реплики."""
        from command_agent_memory.synthetic import load_dialogues

        scenario = load_replay_scenario(ROOT / "docs/examples/replay-anna/dialogue-scenario.json")
        original = load_dialogues(ROOT)[0]["messages"]
        self.assertEqual(scenario.messages, tuple(original))
        self.assertEqual(scenario.request, original[0])
        self.assertNotIn(original[1], scenario.request)

    def test_workspace_contains_only_selected_materials(self):
        """Копирует входы и память; проверяет отсутствие истории, эталона и старых результатов."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, memory, team = root / "inputs", root / "memory", root / "team"
            inputs.mkdir()
            memory.mkdir()
            team.mkdir()
            (inputs / "data.csv").write_text("value\n1", encoding="utf-8")
            (memory / "AGENTS.md").write_text("Правило", encoding="utf-8")
            (memory / "chat_logs.db").write_bytes(b"old chat")
            (memory / "answer.md").write_text("старый результат", encoding="utf-8")
            (team / "INDEX.md").write_text("Команда", encoding="utf-8")
            prepare_workspace(root / "full", inputs, memory, team)
            prepare_workspace(root / "none", inputs)
            self.assertTrue((root / "full/AGENTS.md").is_file())
            self.assertTrue((root / "full/team_knowledge/INDEX.md").is_file())
            self.assertFalse((root / "full/chat_logs.db").exists())
            self.assertFalse((root / "full/answer.md").exists())
            self.assertEqual(list((root / "none").iterdir()), [root / "none/inputs"])

    def test_nested_team_knowledge_is_connected_at_expected_root(self):
        """Копирует командный снимок с knowledge/; подтверждает рабочий путь индекса."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, memory, team = root / "inputs", root / "memory", root / "team"
            inputs.mkdir()
            memory.mkdir()
            (team / "knowledge/skills/process").mkdir(parents=True)
            (team / "knowledge/INDEX.md").write_text("Командный индекс", encoding="utf-8")
            prepare_workspace(root / "full", inputs, memory, team)
            self.assertTrue((root / "full/team_knowledge/INDEX.md").is_file())
            self.assertTrue((root / "full/team_knowledge/skills/process").is_dir())


class ScriptedSubject:
    """Получает подготовленные ответы; хранит входы и выдаёт события одного агента."""

    def __init__(self, workspace, fail=False, mutate=False):
        """Принимает область и варианты сбоя; создаёт журнал вызовов."""
        self.workspace, self.fail, self.mutate = workspace, fail, mutate
        self.calls = []

    def stream(self, inputs, config, stream_mode):
        """Принимает реплику и настройки; выдаёт ответ либо частичный ответ со сбоем."""
        from langchain_core.messages import AIMessage

        self.calls.append((inputs, config))
        for callback in config["callbacks"]:
            callback.on_chat_model_start({}, [])
        yield {"model": {"messages": [AIMessage(content="Наблюдаемый ответ")]}}
        if self.mutate:
            (self.workspace / "AGENTS.md").write_text("Изменено", encoding="utf-8")
        if self.fail:
            raise RuntimeError("Прерывание модели")


@unittest.skipUnless(HAS_RUNTIME, "Для проверки диалога нужно окружение DeepAgents")
class DialogueReplayTests(unittest.TestCase):
    """Проверяет фактические границы данных и управление двумя контекстами."""

    def setUp(self):
        """Создаёт чистую область, эталон и имитации двух участников диалога."""
        from command_agent_memory.evaluation import dialogue
        from replay_worker import ReplayMetrics

        self.module = dialogue
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace, self.reference = self.root / "workspace", self.root / "reference"
        self.workspace.mkdir()
        self.reference.mkdir()
        (self.workspace / "inputs").mkdir()
        (self.workspace / "inputs/data.csv").write_text("value\n1", encoding="utf-8")
        (self.workspace / "AGENTS.md").write_text("Правило памяти", encoding="utf-8")
        (self.reference / "answer.md").write_text("ЭТАЛОН_ТОЛЬКО_ТЕСТИРОВЩИКУ", encoding="utf-8")
        messages = ("Проверь данные", "Уточнение смысла", "Посчитай", "Сохрани SQL")
        self.scenario = ReplayScenario(messages[0], self.workspace / "inputs", self.reference,
                                       messages=messages, goal="Получить результат и SQL")
        self.subject = ScriptedSubject(self.workspace)
        self.observations = []
        self.decisions = [
            dialogue.TesterDecision(action="send", message_index=3, goal_reached=False,
                                    summary="Смысл уже учтён", evidence=["Ответ содержит определение"]),
            dialogue.TesterDecision(action="complete", message_index=None, goal_reached=True,
                                    summary="Цель достигнута", evidence=["Результат и SQL готовы"]),
        ]
        self.tester_metrics = ReplayMetrics()

    def tearDown(self):
        """Освобождает временные данные после завершения каждого испытания."""
        self.temporary.cleanup()

    def execute(self, **kwargs):
        """Подставляет имитации моделей; возвращает результат настоящего управляющего цикла."""
        outer = self

        class Tester:
            """Хранит отдельные наблюдения тестировщика вместо сетевых запросов."""

            def __init__(self, scenario, model, directory, artifact_text_limit):
                """Принимает закрытый сценарий; создаёт отдельные счётчики тестировщика."""
                self.metrics = outer.tester_metrics
                self.duration = 0.0
                directory.mkdir()
                outer.assertEqual(scenario.reference, outer.reference)

            def decide(self, observation):
                """Принимает наблюдение; возвращает очередное проверочное решение."""
                outer.observations.append(observation)
                self.metrics.on_chat_model_start({}, [])
                return outer.decisions.pop(0)

        with patch.object(self.module, "create_agent", return_value=self.subject) as factory:
            with patch.object(self.module, "OpenRouterTester", Tester):
                result = self.module.run_dialogue(self.workspace, self.scenario, self.root / "result",
                                                  "test-model", **kwargs)
        self.factory_call = factory.call_args
        return result

    def test_separate_messages_skipping_and_context(self):
        """Проверяет точную первую реплику, пропуск уточнения и сохранение экземпляра."""
        result = self.execute()
        actual = [call[0]["messages"][0].content for call in self.subject.calls]
        self.assertEqual(actual, ["Проверь данные", "Посчитай"])
        self.assertEqual(self.subject.calls[0][1]["configurable"], self.subject.calls[1][1]["configurable"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["tested_agent"]["model_calls"], 2)
        self.assertEqual(result["tester"]["model_calls"], 2)
        transcript = (self.root / "result/transcript.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("ЭТАЛОН_ТОЛЬКО_ТЕСТИРОВЩИКУ", transcript)
        self.assertNotIn("Смысл уже учтён", transcript)
        self.assertTrue(self.factory_call.kwargs["memory_enabled"])
        self.assertNotIn("system_prompt", self.factory_call.kwargs)
        initialization = json.loads((self.root / "result/initialization.json").read_text(encoding="utf-8"))
        self.assertIsNone(initialization["system_prompt"])
        self.assertEqual(initialization["system_prompt_source"], "deepagents_default")

    def test_subject_failure_keeps_trace_and_metrics(self):
        """Прерывает ответ; подтверждает сохранение частичной трассы и отдельный статус сбоя."""
        self.subject.fail = True
        with self.assertRaisesRegex(RuntimeError, "Прерывание модели"):
            self.execute()
        result = json.loads((self.root / "result/metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["tested_agent"]["model_calls"], 1)
        self.assertEqual(result["tester"]["model_calls"], 0)
        self.assertIn("Наблюдаемый ответ", (self.root / "result/transcript.jsonl").read_text(encoding="utf-8"))

    def test_repeated_or_backward_message_is_rejected(self):
        """Возвращает номер прежней реплики; проверяет остановку до повторного сообщения."""
        self.decisions[0] = self.module.TesterDecision(action="send", message_index=1, goal_reached=False,
                                                      summary="Повтор", evidence=["Причина"])
        with self.assertRaisesRegex(ValueError, "вперёд"):
            self.execute()
        self.assertEqual(len(self.subject.calls), 1)

    def test_turn_limit_is_distinct_from_success(self):
        """Ограничивает опыт одним ходом; проверяет явное отсутствие успешного исхода."""
        result = self.execute(max_user_turns=1)
        self.assertEqual(result["state"], "turn_limit")
        self.assertEqual(len(self.subject.calls), 1)

    def test_memory_mutation_stops_evaluation(self):
        """Изменяет файл памяти; проверяет остановку перед решением тестировщика."""
        self.subject.mutate = True
        with self.assertRaisesRegex(RuntimeError, "изменение"):
            self.execute()
        self.assertEqual(self.observations, [])

    def test_new_memory_file_is_detected(self):
        """Создаёт файл памяти во время ответа; проверяет остановку перед оценкой результата."""
        original = self.subject.stream

        def stream(inputs, config, stream_mode):
            """Принимает вход агента; добавляет новую память и выдаёт прежние события."""
            (self.workspace / "USER_PREFERENCES.md").write_text("Новая запись", encoding="utf-8")
            yield from original(inputs, config, stream_mode)

        self.subject.stream = stream
        with self.assertRaisesRegex(RuntimeError, "изменение"):
            self.execute()
        self.assertEqual(self.observations, [])

    def test_protocol_requires_goal_and_exact_fields(self):
        """Проверяет обрамлённый JSON, строгие типы и обязательное подтверждение цели."""
        decision = self.decisions[0]
        parsed = self.module.parse_tester_decision("```json\n" + decision.model_dump_json() + "\n```")
        self.assertEqual(parsed, decision)
        with self.assertRaises(ValueError):
            self.module.parse_tester_decision('{"action":"complete","goal_reached":true}')
        self.decisions[0] = self.module.TesterDecision(action="complete", message_index=None, goal_reached=False,
                                                      summary="Пробел", evidence=["Данных пока мало"])
        with self.assertRaisesRegex(ValueError, "подтверждённой"):
            self.execute()

    def test_factory_initializes_memory_and_virtual_paths(self):
        """Собирает настоящий backend; проверяет память при инициализации и разрешение путей."""
        from agent_runtime import create_agent

        (self.workspace / "knowledge/skills").mkdir(parents=True)
        (self.workspace / "knowledge/INDEX.md").write_text("Индекс", encoding="utf-8")
        with patch("deepagents.create_deep_agent") as factory, patch("langchain_openai.ChatOpenAI"), patch(
                "openrouter_client.openrouter_api_key", return_value="test-key"):
            create_agent(self.workspace, model_name="test")
            configured = factory.call_args.kwargs
            self.assertNotIn("system_prompt", configured)
            self.assertEqual(configured["memory"], ["/AGENTS.md", "/knowledge/INDEX.md"])
            self.assertEqual(configured["skills"], ["/knowledge/skills/"])
            downloaded = configured["backend"].download_files(["/inputs/data.csv"])[0]
            self.assertIsNone(downloaded.error)
            self.assertEqual(downloaded.content, (self.workspace / "inputs/data.csv").read_bytes())
            create_agent(self.workspace, model_name="test", memory_enabled=False)
            self.assertEqual(factory.call_args.kwargs["memory"], [])
            self.assertEqual(factory.call_args.kwargs["skills"], [])

    def test_real_tester_has_private_reference_and_continues_context(self):
        """Имитирует только транспорт модели; проверяет закрытый эталон и историю тестировщика."""
        from langchain_core.messages import AIMessage

        inputs = []

        def respond(messages, config):
            """Запоминает вход отдельной модели; возвращает подготовленный JSON решения."""
            inputs.append([message.content for message in messages])
            return AIMessage(content=self.decisions[len(inputs) - 1].model_dump_json())

        with patch("langchain_openai.ChatOpenAI") as factory, patch.object(
                self.module, "openrouter_api_key", return_value="test-key"):
            factory.return_value.bind.return_value.invoke.side_effect = respond
            tester = self.module.OpenRouterTester(self.scenario, "tester-model", self.root / "tester")
            tester.decide({"agent_answer": "Первый ответ"})
            tester.decide({"agent_answer": "Второй ответ"})
        self.assertIn("ЭТАЛОН_ТОЛЬКО_ТЕСТИРОВЩИКУ", inputs[0][0])
        self.assertIn("Уточнение смысла", inputs[0][0])
        self.assertEqual(len(inputs[0]), 2)
        self.assertEqual(len(inputs[1]), 4)
        self.assertIn("Первый ответ", inputs[1][1])

    def test_artifact_excerpt_records_truncation_and_skips_unchanged_files(self):
        """Передаёт лимит символов; проверяет явное сокращение и доступность только новых результатов."""
        baseline = self.module.file_hashes(self.workspace)
        (self.workspace / "report.md").write_text("Большой отчёт", encoding="utf-8")
        artifacts = self.module.collect_artifacts(self.workspace, baseline, text_limit=4)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["path"], "report.md")
        self.assertTrue(artifacts[0]["truncated"])
        self.assertEqual(len(artifacts[0]["text"]), 4)


if __name__ == "__main__":
    unittest.main()
