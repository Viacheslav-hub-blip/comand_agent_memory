"""Проверки областей записи и бюджетов без обращений к внешним моделям.

ExecutionPolicyTests содержит test_context_restores_policy, test_scoped_writes,
test_process_deadline, test_review_budget_defaults_and_override,
test_model_call_limit, test_worker_timeout_keeps_metrics, test_timeout_blocks_publication,
test_windows_tool_paths и test_machine_description_matches_operating_system.
test_worker_stops_at_model_budget_before_graph_limit проверяет производственный
граф OpenRouter с повторяющимися вызовами инструментов без сети.
Вложенные repeat и timed_out имитируют инструмент и исчерпание времени.
"""
import json
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import subprocess
import sys
import tempfile
import time
import os
import unittest
from unittest.mock import patch

from command_agent_memory.model_execution.execution_policy import (
    current_execution_policy, execution_policy, run_bounded_process,
)
from command_agent_memory.model_execution.scoped_filesystem import ScopedFilesystemBackend
from command_agent_memory.model_execution.stage_runner import StageRuntime
from command_agent_memory.memory_update.personal_memory_update import consolidate_with_review
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.skill_resources import get_skill_path
from command_agent_memory.model_execution.runtime_environment import describe_execution_environment


class ExecutionPolicyTests(unittest.TestCase):
    """Без внешних сервисов проверяет ограничения этапов и сохранность действующей памяти."""

    def test_context_restores_policy(self):
        """Без входов проверяет вложенные бюджеты, области записи и восстановление после ошибки."""
        original = current_execution_policy()
        with execution_policy(write_paths=("allowed",), timeout_seconds=5):
            with execution_policy(max_model_calls=2):
                self.assertEqual(current_execution_policy().write_paths, ("allowed",))
                self.assertEqual(current_execution_policy().timeout_seconds, 5)
            self.assertIsNone(current_execution_policy().max_model_calls)
            with self.assertRaises(ValueError):
                with execution_policy(timeout_seconds=0):
                    self.fail("Нулевой бюджет принят")
        self.assertEqual(current_execution_policy(), original)

    def test_machine_description_matches_operating_system(self):
        """Без входов проверяет отдельные инструкции Windows и Linux, включая оболочку и пути."""
        with patch("platform.system", return_value="Windows"), patch.dict(os.environ, {"COMSPEC": "cmd.exe"}):
            windows = describe_execution_environment(PureWindowsPath("C:/agents/alice"), file_paths="scoped")
            self.assertIn("Операционная система: Windows", windows)
            self.assertIn("/drives/C/", windows)
            self.assertIn("C:/agents/alice", windows)
            native = describe_execution_environment(PureWindowsPath("C:/agents/alice"), file_paths="native")
            self.assertIn("/drives/C/", native)
            virtual = describe_execution_environment(PureWindowsPath("C:/agents/alice"), file_paths="virtual", shell=True)
            self.assertIn("виртуальные пути", virtual)
            self.assertIn("cmd.exe", virtual)
        with patch("platform.system", return_value="Linux"):
            linux = describe_execution_environment(PurePosixPath("/srv/agents/alice"), file_paths="scoped", shell=True)
            self.assertIn("Операционная система: Linux", linux)
            self.assertIn("/srv/agents/alice", linux)
            self.assertIn("/bin/sh", linux)
            self.assertNotIn("/drives/", linux)
            self.assertNotIn("Windows", linux)

    def test_scoped_writes(self):
        """Без входов проверяет запись, замену, удаление и загрузку внутри и вне knowledge/."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "AGENTS.md"
            rules.write_text("rules", encoding="utf-8")
            backend = ScopedFilesystemBackend(root, (str(root / "knowledge"),))
            self.assertIsNotNone(backend.write(str(root / "outside.md"), "bad").error)
            self.assertIsNotNone(backend.edit(str(rules), "rules", "bad").error)
            self.assertIsNotNone(backend.delete(str(rules)).error)
            self.assertIsNotNone(backend.upload_files([(str(rules), b"bad")])[0].error)
            self.assertIsNone(backend.write(str(root / "knowledge/INDEX.md"), "index").error)
            self.assertIsNone(backend.edit(str(root / "knowledge/INDEX.md"), "index", "changed").error)
            self.assertIsNotNone(backend.write(str(root / "knowledge/../escape.md"), "bad").error)
            self.assertFalse(hasattr(backend, "execute"))
            self.assertEqual(rules.read_text(encoding="utf-8"), "rules")
            self.assertEqual((root / "knowledge/INDEX.md").read_text(encoding="utf-8"), "changed")

    def test_process_deadline(self):
        """Без входов запускает спящий процесс; проверяет остановку по времени без модели."""
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded_process([sys.executable, "-c", "import time; time.sleep(30)"], "", 1)
        self.assertLess(time.monotonic() - started, 8)

    @unittest.skipUnless(os.name == "nt", "Проверка представления дисков Windows")
    def test_windows_tool_paths(self):
        """Без входов проверяет чтение и ограниченную запись через путь /drives/C/... ."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "AGENTS.md"
            rules.write_text("rules", encoding="utf-8")
            backend = ScopedFilesystemBackend(root, (str(root / "knowledge"),))
            alias = "/drives/" + root.drive[0] + root.as_posix()[2:]
            result = backend.read(alias + "/AGENTS.md")
            self.assertIsNone(result.error)
            self.assertIn("rules", result.file_data["content"])
            self.assertIsNotNone(backend.edit(alias + "/AGENTS.md", "rules", "bad").error)
            self.assertIsNone(backend.write(alias + "/knowledge/INDEX.md", "index").error)
            self.assertIsNotNone(backend.write(alias + "/knowledge/../../escape.md", "bad").error)

    def test_review_budget_defaults_and_override(self):
        """Без входов проверяет значения бюджета, их переопределение и отказ для неверных чисел."""
        config = {"defaults": {"provider": "openrouter", "model": "test"}}
        runtime = StageRuntime(config, Path.cwd())
        self.assertEqual(runtime.settings["review"].timeout_seconds, 300)
        self.assertEqual(runtime.settings["review"].max_model_calls, 24)
        config["stages"] = {"review": {"timeout_seconds": 10, "max_model_calls": 2}}
        runtime = StageRuntime(config, Path.cwd())
        self.assertEqual(runtime.settings["review"].timeout_seconds, 10)
        with tempfile.TemporaryDirectory() as temporary, patch.object(runtime, "adapter") as adapter:
            adapter.return_value.agent_runner.side_effect = lambda *args, **kwargs: current_execution_policy()
            result = runtime.run("review", "input", Path.cwd(), Path(temporary) / "request", agent=True)
        self.assertEqual(result.max_model_calls, 2)
        self.assertEqual(result.timeout_seconds, 10)
        for value in (0, -1, True, None, "3"):
            config["stages"]["review"]["timeout_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                StageRuntime(config, Path.cwd())

    def test_model_call_limit(self):
        """Без входов проверяет остановку настоящего графа после двух модельных вызовов."""
        from langchain.agents import create_agent
        from langchain.agents.middleware import ModelCallLimitMiddleware
        from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        response = AIMessage(content="", tool_calls=[{"id": "again", "name": "repeat", "args": {}}])
        model = GenericFakeChatModel(messages=iter([response] * 5))

        def repeat() -> str:
            """Без входов возвращает текст для следующего шага имитируемой модели."""
            return "continue"

        with patch.object(GenericFakeChatModel, "bind_tools", return_value=model):
            agent = create_agent(model, tools=[repeat], middleware=[
                ModelCallLimitMiddleware(run_limit=2, exit_behavior="error")])
            with self.assertRaises(ModelCallLimitExceededError):
                agent.invoke({"messages": [{"role": "user", "content": "repeat"}]})

    def test_worker_timeout_keeps_metrics(self):
        """Без входов имитирует тайм-аут исполнителя OpenRouter; проверяет неполные метрики."""
        from command_agent_memory.model_execution.openrouter_api import run_openrouter

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "review"
            with execution_policy(timeout_seconds=1, max_model_calls=2), patch(
                    "command_agent_memory.model_execution.openrouter_api.run_bounded_process",
                    side_effect=subprocess.TimeoutExpired("worker", 1)):
                with self.assertRaises(TimeoutError):
                    run_openrouter("", "test", "task", root, prefix, writable=True)
            metrics = json.loads(prefix.with_suffix(".metrics.json").read_text(encoding="utf-8"))
            self.assertFalse(metrics["completed"])
            self.assertEqual(metrics["limit_exceeded"], "timeout")
            self.assertFalse(metrics["usage_complete"])

    def test_worker_stops_at_model_budget_before_graph_limit(self):
        """Без сети запускает граф с повтором инструмента; проверяет остановку по бюджету модели."""
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
        from command_agent_memory.model_execution.openrouter_api import _run_openrouter

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alias = "/drives/" + root.drive[0] + root.as_posix()[2:] if root.drive else root.as_posix()
            response = AIMessage(content="", tool_calls=[{"id": "again", "name": "ls", "args": {"path": alias}}])
            model = GenericFakeChatModel(messages=iter([response] * 40))
            with patch("command_agent_memory.model_execution.openrouter_api.validate_openrouter"), patch(
                    "command_agent_memory.model_execution.openrouter_api.openrouter_api_key", return_value="test-key"), patch(
                    "langchain_openai.ChatOpenAI", return_value=model), patch.object(
                    GenericFakeChatModel, "bind_tools", return_value=model), execution_policy(
                    write_paths=(), max_model_calls=20):
                with self.assertRaises(ModelCallLimitExceededError):
                    _run_openrouter("", "fake-model", "repeat", root, root / "worker")
            metrics = json.loads((root / "worker.metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["limit_exceeded"], "model_calls")
            self.assertFalse(metrics["completed"])

    def test_timeout_blocks_publication(self):
        """Без входов имитирует тайм-аут ревью; проверяет статус failed и прежнюю память."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "agent"
            target.mkdir()
            (target / "AGENTS.md").write_text("original", encoding="utf-8")
            calls = []

            def timed_out(executable, model, prompt, cwd, prefix, writable=False):
                """Принимает задание; создаёт предложение, а на ревью имитирует исчерпание времени."""
                calls.append(prefix.name)
                if prefix.name == "review-001":
                    self.assertIn('"candidate": "candidate"', prompt)
                    raise TimeoutError("Исчерпан бюджет ревью")
                (cwd / "memory/AGENTS.md").write_text("candidate", encoding="utf-8")
                return "updated"

            with self.assertRaises(TimeoutError):
                consolidate_with_review(target, "personal", "source", get_skill_path("personal-memory-consolidation"),
                                        "", "test", root / "run", layout=MemoryLayout(("AGENTS.md",)), runner=timed_out)
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), "original")
            status = json.loads((root / "run/status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
