"""Проверяет измерения Codex и сохранение диалогов без запросов к моделям.

Вход: временные файлы, события CLI и сообщения LangChain. Выход: unittest.
Класс CodexClientTests: setUp, tearDown, invoke — подготовка и имитация процесса;
test_usage_and_duration — измерения; test_missing_usage — отсутствие токенов;
test_incomplete_or_failed_turn — отказ; test_timeout_keeps_metrics — таймаут.
В invoke вложенная emit записывает события имитируемого процесса.
Класс ScriptedAgent: __init__ — события; stream — выдача событий или сбой.
Класс AgentRuntimeTests: test_messages_and_sessions — роли, инструменты и контекст;
test_failure_keeps_received_messages — сохранность при сбое.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import agent_runtime
import codex_client


class CodexClientTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет измерения и ошибки запуска, возвращает результаты."""

    def setUp(self):
        """Без входов создаёт каталог, события и префикс; возвращает None."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "request"
        self.events = [{"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 8}}]
        self.return_code = 0
        self.timeout = False

    def tearDown(self):
        """Без входов освобождает временный каталог; возвращает None."""
        self.temporary.cleanup()

    def invoke(self):
        """Без входов вызывает клиент с имитацией процесса; возвращает ответ или исключение."""
        def emit(command, **kwargs):
            """Принимает команду и параметры процесса; записывает события, возвращает CompletedProcess."""
            for event in self.events:
                kwargs["stdout"].write(json.dumps(event) + "\n")
            if self.timeout:
                raise subprocess.TimeoutExpired(command, 900)
            Path(command[command.index("-o") + 1]).write_text("Ответ", encoding="utf-8")
            return subprocess.CompletedProcess(command, self.return_code)

        with patch.object(codex_client.subprocess, "run", side_effect=emit):
            return codex_client.run_codex("codex", "test-model", "Задание", self.root, self.prefix)

    def test_usage_and_duration(self):
        """Без входов проверяет сумму usage двух turns без двойного учёта кеша; возвращает None."""
        self.events = [
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 8}},
            {"type": "turn.completed", "usage": {"input_tokens": 30, "cached_input_tokens": 5, "output_tokens": 2}},
        ]
        self.assertEqual(self.invoke(), "Ответ")
        metrics = json.loads(self.prefix.with_suffix(".metrics.json").read_text())
        self.assertEqual(metrics["usage"], {"input_tokens": 130, "cached_input_tokens": 25, "output_tokens": 10})
        self.assertGreaterEqual(metrics["duration_seconds"], 0)
        self.assertTrue(metrics["completed"])

    def test_missing_usage(self):
        """Без входов проверяет null вместо выдуманного числа токенов; возвращает None."""
        self.events = [{"type": "turn.completed"}]
        self.invoke()
        metrics = json.loads(self.prefix.with_suffix(".metrics.json").read_text())
        self.assertIsNone(metrics["usage"])

    def test_incomplete_or_failed_turn(self):
        """Без входов проверяет отказ при нулевом коде без завершения или с ошибкой; возвращает None."""
        for events in ([{"type": "turn.started"}], [{"type": "turn.completed"}, {"type": "error"}]):
            with self.subTest(events=events):
                self.events = events
                with self.assertRaises(RuntimeError):
                    self.invoke()
                metrics = json.loads(self.prefix.with_suffix(".metrics.json").read_text())
                self.assertFalse(metrics["completed"])

    def test_timeout_keeps_metrics(self):
        """Без входов проверяет сохранение времени при таймауте; возвращает None."""
        self.events = [{"type": "turn.started"}]
        self.timeout = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke()
        metrics = json.loads(self.prefix.with_suffix(".metrics.json").read_text())
        self.assertFalse(metrics["completed"])
        self.assertIsNone(metrics["return_code"])
        self.assertIsNone(metrics["usage"])

class ScriptedAgent:
    """Принимает заранее заданные события; выдаёт их вместо вызова модели."""

    def __init__(self, messages, fail=False):
        """Принимает сообщения и признак сбоя; создаёт список вызовов, возвращает None."""
        self.messages = messages
        self.fail = fail
        self.calls = []

    def stream(self, inputs, config, stream_mode):
        """Принимает вход и конфигурацию; выдаёт обновления, затем при необходимости вызывает ошибку."""
        self.calls.append((inputs, config, stream_mode))
        for message in self.messages:
            yield {"model": {"messages": [message]}}
        if self.fail:
            raise RuntimeError("Сбой модели")


@unittest.skipUnless(importlib.util.find_spec("langchain_core"), "Нужно окружение с LangChain")
class AgentRuntimeTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет настоящие SQLite и сообщения LangChain без модели."""

    def test_messages_and_sessions(self):
        """Без входов проверяет роли, вызовы инструментов, usage и продолжение сеанса; возвращает None."""
        from langchain_core.messages import AIMessage, ToolMessage

        answer = AIMessage(
            content="Проверяю", tool_calls=[{"id": "call-1", "name": "read_file", "args": {"path": "input.csv"}}],
            usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        )
        tool = ToolMessage(content="Данные", tool_call_id="call-1")
        agent = ScriptedAgent([answer, tool])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_logs.db"
            connection = agent_runtime.open_chat_database(path)
            try:
                self.assertEqual(list(agent_runtime.run_turn(agent, "Запрос", connection, "session-1", "101")), [answer, tool])
                agent.messages = [AIMessage(content="Продолжение")]
                list(agent_runtime.run_turn(agent, "Ещё", connection, "session-1", "101"))
                rows = connection.execute("SELECT role, message_json FROM messages ORDER BY id").fetchall()
                self.assertEqual([row[0] for row in rows], ["user", "ai", "tool", "user", "ai"])
                payload = json.loads(rows[1][1])["data"]
                self.assertEqual(payload["tool_calls"][0]["id"], "call-1")
                self.assertEqual(payload["usage_metadata"]["total_tokens"], 13)
                self.assertEqual(agent.calls[0][1], agent.calls[1][1])
            finally:
                connection.close()
            connection = agent_runtime.open_chat_database(path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 5)
            finally:
                connection.close()

    def test_failure_keeps_received_messages(self):
        """Без входов проверяет сохранность реплики и частичного ответа после сбоя; возвращает None."""
        from langchain_core.messages import AIMessage

        agent = ScriptedAgent([AIMessage(content="Начал")], fail=True)
        with tempfile.TemporaryDirectory() as directory:
            connection = agent_runtime.open_chat_database(Path(directory) / "chat_logs.db")
            try:
                with self.assertRaisesRegex(RuntimeError, "Сбой модели"):
                    list(agent_runtime.run_turn(agent, "Запрос", connection, "session-1", "101"))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
