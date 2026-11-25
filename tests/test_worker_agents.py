"""Проверки штатной сборки и сопоставимости replay без сетевых моделей.

Функции и методы:
setUp;
tearDown;
test_factory_creates_independent_equal_sessions;
test_changed_tools_model_prompt_and_environment_are_rejected;
test_reused_agent_instance_is_rejected;
test_missing_agent_has_no_generic_fallback;
test_incomplete_profile_closes_resources;
test_specification_validation_does_not_import_factory;
test_shared_factory_keeps_real_sql_shell_and_dialogue_state;
transport;
bind;
run_sql;
"""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from command_agent_memory.worker_agents import (
    WorkerSession, load_worker_session, profile_fingerprint, require_same_profile, validate_agent_spec,
)
from worker_fixture import profile


class WorkerAgentTests(unittest.TestCase):
    """Проверяет реальные графы и инструменты с управляемыми ответами модели."""

    def setUp(self):
        """Без входов создаёт две независимые рабочие копии; возвращает None."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspaces = [self.root / name for name in ("before", "after")]
        for workspace in self.workspaces:
            workspace.mkdir()
        self.specification = {"factory": "worker_fixture:create_session", "options": {"model": "original-model"}}

    def tearDown(self):
        """Без входов удаляет тестовые файлы; возвращает None."""
        self.temporary.cleanup()

    def test_factory_creates_independent_equal_sessions(self):
        """Загружает одну штатную фабрику дважды; проверяет равенство профилей и независимость экземпляров."""
        before = load_worker_session(self.specification, self.workspaces[0])
        after = load_worker_session(self.specification, self.workspaces[1])
        self.assertIsNot(before.agent, after.agent)
        self.assertEqual(require_same_profile(before, after), profile_fingerprint(before.profile))
        self.assertEqual(before.profile["model"], "original-model")
        before.close()
        before.close()
        after.close()
        self.assertTrue(before.agent.closed and after.agent.closed)

    def test_changed_tools_model_prompt_and_environment_are_rejected(self):
        """Изменяет модель, схему, реализацию, инструкции и среду; проверяет запрет сравнения."""
        original = profile()
        variants = [deepcopy(original) for _ in range(5)]
        variants[0]["model"] = "other"
        variants[1]["tools"][0]["schema"] = {"type": "string"}
        variants[2]["tools"][0]["implementation"] = "other-version"
        variants[3]["system_prompt_sha256"] = "0" * 64
        variants[4]["environment"]["resource_snapshot"] = "changed"
        for changed in variants:
            with self.subTest(profile=changed), self.assertRaisesRegex(RuntimeError, "Профили"):
                require_same_profile(WorkerSession(Mock(), original), WorkerSession(Mock(), changed))

    def test_reused_agent_instance_is_rejected(self):
        """Возвращает один экземпляр для обоих прогонов; проверяет отказ от общего состояния."""
        agent = Mock()
        with self.assertRaisesRegex(RuntimeError, "независимый"):
            require_same_profile(WorkerSession(agent, profile()), WorkerSession(agent, profile()))

    def test_missing_agent_has_no_generic_fallback(self):
        """Передаёт отсутствующую фабрику; проверяет ошибку вместо подмены рабочего агента."""
        with self.assertRaisesRegex(ValueError, "agent"):
            load_worker_session(None, self.workspaces[0])
        with self.assertRaises(ModuleNotFoundError):
            load_worker_session({"factory": "missing_worker_module:create_session"}, self.workspaces[0])

    def test_incomplete_profile_closes_resources(self):
        """Фабрика возвращает неполный профиль; проверяет отказ и освобождение её ресурсов."""
        cleanup = Mock()
        with patch("worker_fixture.create_session", return_value=WorkerSession(Mock(), {}, cleanup)):
            with self.assertRaisesRegex(ValueError, "неполный профиль"):
                load_worker_session(self.specification, self.workspaces[0])
        cleanup.assert_called_once_with()

    def test_specification_validation_does_not_import_factory(self):
        """Проверяет конфигурацию без выполнения пользовательского Python-кода."""
        with patch("command_agent_memory.worker_agents.importlib.import_module") as load:
            validate_agent_spec(self.specification)
        load.assert_not_called()

    def test_legacy_model_stage_requires_explicit_worker_migration(self):
        """Проверяет понятную ошибку старой настройки вместо выбора другого рабочего агента."""
        from command_agent_memory.model_execution.stage_runner import StageRuntime

        with self.assertRaisesRegex(ValueError, "agent.factory/options"):
            StageRuntime({"stages": {"replay_agent": {"provider": "openrouter", "model": "old"}}}, self.root)

    def test_shared_factory_keeps_real_sql_shell_and_dialogue_state(self):
        """Выполняет SQL и shell через реальные tools двух графов; проверяет сохранение штатного контекста."""
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool
        from command_agent_memory.working_agent import create_session
        from command_agent_memory.memory_replay.replay_agent import open_chat_database, run_turn

        def transport(**kwargs):
            """Принимает настройки ChatOpenAI; возвращает ответы без сетевых запросов."""
            return GenericFakeChatModel(messages=iter([
                AIMessage(content="", tool_calls=[{"id": "sql", "name": "run_sql", "args": {"query": "SELECT 7"}}]),
                AIMessage(content="", tool_calls=[{"id": "shell", "name": "execute", "args": {"command": "echo native> shell-result.txt"}}]),
                AIMessage(content="Первый ответ"), AIMessage(content="Второй ответ"),
            ]))

        def bind(model, *args, **kwargs):
            """Принимает тестовую модель и схемы tools; возвращает тот же транспорт."""
            return model

        sessions = []
        with patch("langchain_openai.ChatOpenAI", side_effect=transport) as models, patch(
                "command_agent_memory.model_execution.openrouter_api.openrouter_api_key", return_value="test-key"), patch.object(
                GenericFakeChatModel, "bind_tools", autospec=True, side_effect=bind):
            for index, workspace in enumerate(self.workspaces):
                @tool
                def run_sql(query: str) -> str:
                    """Принимает SQL-запрос; возвращает строки независимой SQLite-копии задачи."""
                    with closing(sqlite3.connect(workspace / "data.db")) as connection:
                        return json.dumps(connection.execute(query).fetchall())

                (workspace / "AGENTS.md").write_text("old" if index == 0 else "new", encoding="utf-8")
                session = create_session(workspace, {"model": "original-model", "model_parameters": {"temperature": 0},
                                                     "run_config": {"recursion_limit": 120, "max_concurrency": 2}}, tools=[run_sql])
                self.assertEqual(session.agent.config["recursion_limit"], 120)
                self.assertEqual(session.profile["environment"]["run_config"]["max_concurrency"], 2)
                sessions.append(session)
                connection = open_chat_database(self.root / f"chat-{index}.db")
                try:
                    first = list(run_turn(session.agent, "Первый запрос", connection, f"session-{index}", "user"))
                    second = list(run_turn(session.agent, "Второй запрос", connection, f"session-{index}", "user"))
                finally:
                    connection.close()
                self.assertTrue(any(message.type == "tool" and "[[7]]" in message.content for message in first))
                self.assertIn("native", (workspace / "shell-result.txt").read_text())
                messages = session.agent.get_state({"configurable": {"thread_id": f"session-{index}"}}).values["messages"]
                self.assertEqual([message.content for message in messages if message.type == "human"],
                                 ["Первый запрос", "Второй запрос"])
                self.assertEqual(second[-1].content, "Второй ответ")
            self.assertTrue(all(call.kwargs["model"] == "original-model" and call.kwargs["temperature"] == 0
                                for call in models.call_args_list))
        require_same_profile(*sessions)
        names = {tool["name"] for tool in sessions[0].profile["tools"]}
        self.assertTrue({"run_sql", "execute", "read_file", "write_file"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
