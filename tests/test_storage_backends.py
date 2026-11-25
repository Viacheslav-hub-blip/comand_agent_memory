"""Проверки SQL-источника и хранилищ памяти без моделей и сетевых запросов.

SQLSourceTests проверяет параметры SELECT, фильтрацию и закрытие DB-API.
SQLiteStoreTests проверяет снимки, конфликт версий и транзакционный откат.
DatabasePipelineTests запускает полный цикл с SQLite-памятью и фиктивной моделью.
"""
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.model_execution.provider_registry import ModelAdapter
import command_agent_memory.model_execution.provider_registry as registry
from command_agent_memory.memory_pipeline import run_pipeline
from command_agent_memory.history_sources.conversation_history import load_history_source
from command_agent_memory.memory_storage.storage_registry import load_memory_store, register_memory_store
from command_agent_memory.memory_storage import storage_registry as backends
from command_agent_memory.memory_storage.sqlite_storage import SQLiteMemoryStore
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.history_sources.sql_history_source import json_value


class SQLSourceTests(unittest.TestCase):
    """Проверяет DB-API на настоящей SQLite как одном из SQL-драйверов."""

    def test_parameterized_query_filters_sessions_without_writing(self):
        """Без входов проверяет фильтр владельца, параметры и неизменность исходной БД."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE messages(id, session_id, user_id, content)")
                connection.executemany("INSERT INTO messages VALUES(?,?,?,?)",
                                       [(1, "a", "alice", "Запрос"), (2, "b", "bob", "Другая сессия")])
                connection.commit()
            before = path.read_bytes()
            source = load_history_source({"type": "sql", "driver": "sqlite3", "dsn_env": "TEST_MEMORY_DSN",
                "query": "SELECT * FROM messages WHERE user_id = :owner ORDER BY id",
                "parameters": {"owner": "alice"}}, Path(temporary))
            with patch.dict(os.environ, {"TEST_MEMORY_DSN": path.resolve().as_uri() + "?mode=ro"}):
                source.options["connect_kwargs"] = {"uri": True}
                sessions = source.list_sessions()
                events = list(source.iter_events(sessions[0]))
            self.assertEqual(sessions, [{"id": "a"}])
            self.assertEqual(events[0].data["content"], "Запрос")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json_value(Decimal("1.234567890123456789")), "1.234567890123456789")

    def test_cursor_and_connection_close_on_query_failure(self):
        """Без входов проверяет освобождение ресурсов и rollback при исключении драйвера."""
        from unittest.mock import Mock

        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.execute.side_effect = RuntimeError("Ошибка драйвера")
        source = load_history_source({"type": "sql", "driver": "sqlite3", "dsn_env": "TEST_MEMORY_DSN",
                                      "query": "SELECT missing FROM messages"}, Path.cwd())
        with patch.dict(os.environ, {"TEST_MEMORY_DSN": ":memory:"}), patch(
                "sqlite3.connect", return_value=connection), self.assertRaises(RuntimeError):
            source.list_sessions()
        cursor.close.assert_called_once()
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()


class SQLiteStoreTests(unittest.TestCase):
    """Проверяет контракт реального хранилища и сохранение прежней версии при сбое."""

    def test_registered_backend_uses_same_snapshot_and_publish_contract(self):
        """Без входов подключает фабрику стороннего backend и публикует документы через общий контракт."""
        def factory(options, base, layout):
            """Принимает параметры стороннего backend; возвращает SQLite-реализацию для теста."""
            return SQLiteMemoryStore(base / options["database"], layout.entries)

        with tempfile.TemporaryDirectory() as temporary, patch.dict(backends._FACTORIES):
            register_memory_store("external", factory)
            with self.assertRaises(ValueError):
                register_memory_store("external", factory)
            root = Path(temporary)
            store = load_memory_store({"type": "external", "database": "external.db"},
                                      root, MemoryLayout(("AGENTS.md",)))
            store.publish({}, {"AGENTS.md": b"external"}, root / "run")
            self.assertEqual(store.snapshot(), {"AGENTS.md": b"external"})

    def test_snapshot_does_not_create_database_and_publish_detects_conflicts(self):
        """Без входов проверяет чтение без записи, резервную копию и оптимистическую блокировку."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SQLiteMemoryStore(root / "memory.db", ("AGENTS.md", "notes"))
            self.assertEqual(store.snapshot(), {})
            self.assertFalse(store.path.exists())
            first = {"AGENTS.md": b"first", "notes/case.md": b"case"}
            store.publish({}, first, root / "first")
            self.assertEqual(store.snapshot(), first)
            with self.assertRaisesRegex(RuntimeError, "изменилась"):
                store.publish({}, {"AGENTS.md": b"stale"}, root / "stale")
            self.assertEqual(store.snapshot(), first)
            with self.assertRaisesRegex(ValueError, "вне настроенной"):
                store.publish(first, {"other.md": b"wrong"}, root / "wrong")
            store.publish(first, {"AGENTS.md": b"second"}, root / "second")
            self.assertEqual(store.snapshot(), {"AGENTS.md": b"second"})
            self.assertEqual((root / "second/external-backup/notes/case.md").read_bytes(), b"case")

    def test_failed_second_document_rolls_back_entire_transaction(self):
        """Без входов вызывает SQL-сбой второй записи и проверяет отсутствие частичной публикации."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SQLiteMemoryStore(root / "memory.db", ("notes",))
            before = {"notes/a": b"a", "notes/b": b"b"}
            store.publish({}, before, root / "first")
            with closing(sqlite3.connect(store.path)) as connection:
                connection.execute("CREATE TRIGGER reject_b BEFORE UPDATE ON memory_documents "
                                   "WHEN NEW.path='notes/b' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
                connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.publish(before, {"notes/a": b"new-a", "notes/b": b"new-b"}, root / "failed")
            self.assertEqual(store.snapshot(), before)


class DatabasePipelineTests(unittest.TestCase):
    """Проверяет весь настроенный цикл с БД вместо файловой памяти."""

    def test_database_publication_review_and_cache(self):
        """Без входов проверяет validate-only, публикацию, повторное использование и смену БД."""
        calls = []

        def runner(executable, model, prompt, cwd, prefix, writable=False):
            """Принимает запрос модели; возвращает отчёт или решение, изменяя только тестовую копию."""
            stage = json.loads(prefix.with_suffix(".stage.json").read_text(encoding="utf-8"))["stage"]
            calls.append(stage)
            if stage == "consolidation":
                (cwd / "memory/AGENTS.md").write_bytes(b"accepted")
            if stage == "review":
                return json.dumps({"decision": "accepted", "summary": "Принято", "findings": []})
            return "Отчёт"

        def factory(executable):
            """Принимает необязательный путь CLI; возвращает тестовый адаптер без сети."""
            return ModelAdapter("test-storage", "", runner, runner)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "history.jsonl").write_text(json.dumps({"session_id": "s", "content": "test"}), encoding="utf-8")
            configuration = {"defaults": {"provider": "test-storage", "model": "fake"}, "users": [
                {"user_id": "alice", "workspace": "runs", "source": {"type": "jsonl", "path": "history.jsonl"},
                 "memory": {"type": "sqlite", "path": "memory.db", "personal": ["AGENTS.md"]}}]}
            path = root / "memory_pipeline.json"
            path.write_text(json.dumps(configuration), encoding="utf-8")
            with patch.dict(registry._FACTORIES, {"test-storage": factory}), patch(
                    "socket.socket.connect", side_effect=AssertionError("Сеть запрещена")):
                run_pipeline(path, validate_only=True)
                self.assertEqual(sorted(item.name for item in root.iterdir()), ["history.jsonl", "memory_pipeline.json"])
                run_pipeline(path)
                store = load_memory_store(configuration["users"][0]["memory"], root, MemoryLayout(("AGENTS.md",)))
                self.assertEqual(store.snapshot(), {"AGENTS.md": b"accepted"})
                run_pipeline(path)
                self.assertEqual(calls, ["context", "procedures", "consolidation", "review"])
                configuration["users"][0]["memory"]["path"] = "other.db"
                path.write_text(json.dumps(configuration), encoding="utf-8")
                run_pipeline(path)
                self.assertTrue((root / "other.db").is_file())
                self.assertEqual(calls[-2:], ["consolidation", "review"])


if __name__ == "__main__":
    unittest.main()
