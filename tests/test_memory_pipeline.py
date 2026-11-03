"""Проверка подключений внешних агентов без сетевых запросов.

Классы: ConnectionTests — конфигурация, этапы и публикация;
HistoryTests — форматы, ветки и ошибки; StoreTests — границы, конкуренция и откат.
Методы setUp/tearDown готовят окружение; factory/runner имитируют провайдеров;
write_jsonl создаёт логи; execute запускает цикл; test_* проверяют публичное поведение.
"""
from contextlib import closing, redirect_stdout
import io
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.model_execution.provider_registry import ModelAdapter
import command_agent_memory.model_execution.provider_registry as registry
from command_agent_memory.model_execution.stage_runner import StageRuntime
from command_agent_memory.memory_pipeline import run_pipeline
from command_agent_memory.history_sources.conversation_history import load_history_source, read_jsonl
from command_agent_memory.history_sources import conversation_history as history
from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.team_memory_delivery.local_copy import sync_team_memory


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Принимает путь и записи; создаёт JSONL UTF-8 для проверки адаптера."""
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records), encoding="utf-8")


class ConnectionTests(unittest.TestCase):
    """Проверяет настоящий файловый цикл с имитацией разных моделей и провайдеров."""

    def setUp(self):
        """Без входов создаёт внешний проект, историю и конфигурацию; возвращает None."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "agent"
        self.target.mkdir()
        (self.target / "CLAUDE.md").write_text("Прежние правила", encoding="utf-8")
        (self.target / "unrelated.txt").write_text("Не изменять", encoding="utf-8")
        write_jsonl(self.root / "history.jsonl", [{"session_id": "../../chat", "role": "user", "content": "Задача"}])
        (self.root / "wishes.txt").write_text("Сохраняй термины пользователя.", encoding="utf-8")
        self.configuration = {
            "defaults": {"provider": "fake-a", "model": "model-a"},
            "stages": {"review": {"provider": "fake-b", "model": "model-review"},
                       "knowledge": {"provider": "fake-b", "model": "model-knowledge"}},
            "wishes_file": "wishes.txt",
            "users": [{"user_id": "alice", "workspace": "agent/.memory-runs",
                       "source": {"type": "jsonl", "path": "history.jsonl"},
                       "memory": {"root": "agent", "personal": ["CLAUDE.md"], "knowledge": ["notes"]}}]}
        self.path = self.root / "memory_pipeline.json"
        self.calls = []
        self.reject = False
        self.mutate = False
        self.rogue = False
        self.cross_scope = False
        self.patchers = [patch.dict(registry._FACTORIES, {"fake-a": self.factory, "fake-b": self.factory}),
                         patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена"))]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        """Без входов снимает подмены и удаляет временные данные; возвращает None."""
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def factory(self, executable):
        """Принимает необязательный путь CLI; возвращает локальный адаптер без сети."""
        return ModelAdapter("fake", "", self.runner, self.runner)

    def runner(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает запрос этапа; обновляет копию либо возвращает отчёт и решение ревью."""
        metadata = json.loads(prefix.with_suffix(".stage.json").read_text(encoding="utf-8"))
        stage = metadata["stage"]
        self.calls.append((stage, metadata["provider"], model, prompt))
        if stage == "consolidation":
            (cwd / "memory/CLAUDE.md").write_text("Уточнённые правила", encoding="utf-8")
            if self.rogue:
                (cwd / "memory/rogue.txt").write_text("Лишний файл", encoding="utf-8")
        if stage == "knowledge":
            (cwd / "memory/notes").mkdir(exist_ok=True)
            (cwd / "memory/notes/howto.md").write_text("Сохранённая процедура", encoding="utf-8")
            if self.cross_scope:
                (cwd / "memory/CLAUDE.md").write_text("Правка другого этапа", encoding="utf-8")
        if stage == "review":
            if self.mutate:
                (self.target / "CLAUDE.md").write_text("Изменено рабочим агентом", encoding="utf-8")
            return json.dumps({"decision": "changes_requested" if self.reject else "accepted",
                               "summary": "Решение проверки", "findings": []})
        if stage == "replay_agent":
            self.assertNotIn("SECRET_REFERENCE", prompt)
            self.assertIn("Первая реплика", prompt)
            return "Задача решена"
        if stage == "replay_tester":
            count = sum(call[0] == "replay_tester" for call in self.calls)
            return json.dumps({"action": "send" if count == 1 else "complete",
                               "message_index": 2 if count == 1 else None, "goal_reached": count > 1,
                               "summary": "Следующий ход" if count == 1 else "Цель достигнута",
                               "evidence": ["Ответ получен"]})
        if stage == "evaluation":
            return json.dumps({"choice": "candidate", "summary": "Проверка пройдена"})
        if stage == "team":
            materials = (prefix.parent / "materials.md").read_text(encoding="utf-8")
            self.assertIn("Сохранённая процедура", materials)
            self.assertNotIn("Уточнённые правила", materials)
            self.assertIn("/after/notes/howto.md", materials)
            self.assertNotIn("/after/knowledge/notes/howto.md", materials)
            (cwd / "memory/INDEX.md").write_text("Командный индекс", encoding="utf-8")
            return json.dumps({"decision": "accepted", "summary": "Знания объединены", "findings": []})
        return "Извлечённые сведения"

    def execute(self, **options):
        """Принимает флаги; сохраняет конфигурацию и выполняет подключённый цикл."""
        self.path.write_text(json.dumps(self.configuration), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            run_pipeline(self.path, **options)

    def test_complete_cycle_routes_models_and_updates_external_memory(self):
        """Без входов проверяет реальные записи, разные модели и пожелания на всех этапах."""
        self.execute()
        self.assertEqual([call[0] for call in self.calls], ["context", "procedures", "consolidation", "knowledge", "review"])
        self.assertEqual(self.calls[-1][1:3], ("fake-b", "model-review"))
        self.assertEqual(self.calls[-2][1:3], ("fake-b", "model-knowledge"))
        self.assertTrue(all("Сохраняй термины пользователя." in call[3] for call in self.calls))
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "Уточнённые правила")
        self.assertTrue((self.target / "notes/howto.md").is_file())
        self.assertEqual((self.target / "unrelated.txt").read_text(encoding="utf-8"), "Не изменять")
        self.assertFalse((self.target / "AGENTS.md").exists())
        self.execute()
        self.assertEqual(len(self.calls), 5)
        (self.root / "wishes.txt").write_text("Новые пожелания", encoding="utf-8")
        self.execute()
        self.assertEqual(len(self.calls), 10)

    def test_validate_only_does_not_write_or_load_models(self):
        """Без входов проверяет чтение логов без создания рабочих каталогов и вызовов LLM."""
        self.execute(validate_only=True)
        self.assertFalse((self.target / ".memory-runs").exists())
        self.assertEqual(self.calls, [])

    def test_rejection_keeps_original_memory(self):
        """Без входов проверяет сохранность оригиналов после отказа ревью; возвращает None."""
        self.reject = True
        with self.assertRaisesRegex(RuntimeError, "Память не обновлена"):
            self.execute()
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "Прежние правила")
        self.assertFalse((self.target / "notes").exists())

    def test_concurrent_agent_changes_are_preserved(self):
        """Без входов проверяет отказ от перезаписи новой версии рабочего агента."""
        self.mutate = True
        with self.assertRaisesRegex(RuntimeError, "изменилась"):
            self.execute()
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "Изменено рабочим агентом")

    def test_unconfigured_files_prevent_publication(self):
        """Без входов проверяет отказ при создании файлов за границами настроенной копии."""
        self.rogue = True
        with self.assertRaisesRegex(RuntimeError, "вне настроенной памяти"):
            self.execute()
        self.assertFalse((self.target / "rogue.txt").exists())

    def test_consolidators_respect_individual_scopes(self):
        """Без входов проверяет запрет консолидатору знаний менять личные правила."""
        self.cross_scope = True
        with self.assertRaisesRegex(RuntimeError, "вне области своего этапа"):
            self.execute()
        self.assertEqual((self.target / "CLAUDE.md").read_text(encoding="utf-8"), "Прежние правила")

    def test_extension_module_adds_source_without_changing_pipeline(self):
        """Без входов импортирует отдельный модуль адаптера и выполняет прежний конвейер."""
        module = "connection_test_extension"
        (self.root / (module + ".py")).write_text(
            '\"\"\"Пример подключения собственного источника без новых функций.\"\"\"\n'
            'from command_agent_memory.history_sources.conversation_history import RecordSource, register_history_source\n'
            'register_history_source("custom_history", RecordSource)\n', encoding="utf-8")
        self.configuration["extensions"] = [module]
        self.configuration["users"][0]["source"]["type"] = "custom_history"
        try:
            with patch.object(sys, "path", [str(self.root), *sys.path]), patch.dict(history._SOURCES):
                self.execute()
        finally:
            sys.modules.pop(module, None)
        self.assertEqual(len(self.calls), 5)

    def test_missing_wishes_and_unknown_stage_fail_before_calls(self):
        """Без входов проверяет ошибки конфигурации до записи памяти и вызова модели."""
        self.configuration["wishes_file"] = "absent.txt"
        with self.assertRaises(FileNotFoundError):
            self.execute()
        self.configuration.pop("wishes_file")
        self.configuration["stages"]["typo"] = {"model": "wrong"}
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_wishes_are_loaded_once(self):
        """Без входов проверяет неизменность пожеланий внутри одного экземпляра runtime."""
        runtime = StageRuntime(self.configuration, self.root)
        (self.root / "wishes.txt").write_text("Изменение во время запуска", encoding="utf-8")
        runtime.run("context", "Первый запрос", self.root, self.root / "first")
        runtime.run("procedures", "Второй запрос", self.root, self.root / "second")
        self.assertTrue(all("Сохраняй термины пользователя." in call[3] for call in self.calls))

    def test_team_exports_only_knowledge_and_reuses_accepted_version(self):
        """Без входов проверяет командную модель, отсутствие личных правил и повторный запуск."""
        self.configuration["team"] = {"root": "agent/shared"}
        self.configuration["stages"]["team"] = {"provider": "fake-b", "model": "team-model"}
        self.execute()
        self.assertEqual(self.calls[-1][:3], ("team", "fake-b", "team-model"))
        self.assertTrue((self.target / "shared/INDEX.md").is_file())
        self.execute()
        self.assertEqual(len(self.calls), 6)

    def test_team_delivery_preserves_personal_memory_and_recovers_on_repeat(self):
        """Без входов проверяет доставку A → B, сохранность личного и восстановление копии."""
        recipient = self.root / "bob"
        recipient.mkdir()
        (recipient / "AGENTS.md").write_bytes(b"bob rules")
        self.configuration["team"] = {"root": "agent/shared", "workspaces": ["bob"]}
        self.execute()
        target = recipient / "team_knowledge/INDEX.md"
        self.assertEqual(target.read_text(encoding="utf-8"), "Командный индекс")
        self.assertFalse((recipient / "team_knowledge/CLAUDE.md").exists())
        target.write_bytes(b"broken copy")
        stale = recipient / "team_knowledge/stale.md"
        stale.write_bytes(b"obsolete")
        self.execute()
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(target.read_text(encoding="utf-8"), "Командный индекс")
        self.assertFalse(stale.exists())
        self.assertEqual((recipient / "AGENTS.md").read_bytes(), b"bob rules")
        deliveries = list((self.target / ".memory-runs/team-runs").glob("*/delivery/status.json"))
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["snapshot_sha256"]
                            for path in deliveries))

    def test_invalid_team_delivery_fails_before_models_and_writes(self):
        """Без входов проверяет пересечения, дубли, отсутствующие каталоги и режим validate-only."""
        for recipients in (["agent", "agent"], ["absent"], "agent", ["agent"]):
            with self.subTest(recipients=recipients):
                self.configuration["team"] = {"root": "agent/team_knowledge", "workspaces": recipients}
                with self.assertRaises(ValueError):
                    self.execute(validate_only=True)
        self.configuration["team"] = {"root": "shared", "workspaces": ["agent"]}
        self.execute(validate_only=True)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.target / "team_knowledge").exists())
        self.assertFalse((self.target / ".memory-runs").exists())
        self.configuration["team"]["workspaces"] = ["agent", "agent"]
        with self.assertRaises(ValueError):
            self.execute(validate_only=True)

    def test_rejected_team_update_is_not_delivered(self):
        """Без входов проверяет, что отказ команды сохраняет прежнюю копию получателя."""
        copy = self.target / "team_knowledge"
        copy.mkdir()
        (copy / "INDEX.md").write_bytes(b"previous team version")
        self.configuration["team"] = {"root": "shared", "workspaces": ["agent"]}
        with patch("command_agent_memory.memory_update.team_memory_update.consolidate_team_memory",
                   side_effect=RuntimeError("Командное объединение не принято")):
            with self.assertRaisesRegex(RuntimeError, "не принято"):
                self.execute()
        self.assertEqual((copy / "INDEX.md").read_bytes(), b"previous team version")

    def test_delivery_without_index_does_not_erase_existing_copy(self):
        """Без входов проверяет отказ для неполного источника без удаления прежних знаний."""
        copy = self.target / "team_knowledge"
        copy.mkdir()
        (copy / "INDEX.md").write_bytes(b"previous team version")
        with self.assertRaisesRegex(ValueError, "INDEX.md"):
            sync_team_memory(self.root / "absent", self.target, self.root / "delivery")
        self.assertEqual((copy / "INDEX.md").read_bytes(), b"previous team version")
        self.assertFalse((self.root / "delivery").exists())

    @unittest.skipUnless(importlib.util.find_spec("langchain_core"), "Нужен langchain-core для диалога")
    def test_replay_has_independent_agent_tester_and_judge(self):
        """Без входов проверяет модели всех ходов диалога, пожелания и изоляцию эталона."""
        for name in ("inputs", "reference"):
            (self.root / name).mkdir()
        (self.root / "reference/answer.md").write_text("SECRET_REFERENCE", encoding="utf-8")
        (self.root / "inputs/data.txt").write_text("Данные", encoding="utf-8")
        (self.root / "scenario.json").write_text(json.dumps({"messages": ["Первая реплика", "Вторая реплика"],
             "inputs": "inputs", "reference": "reference", "goal": "Получить ответ"}), encoding="utf-8")
        self.configuration["users"][0]["replay_scenario"] = "scenario.json"
        for name in ("replay_agent", "replay_tester", "evaluation"):
            self.configuration["stages"][name] = {"provider": "fake-b", "model": name + "-model"}
        self.execute(evaluate_by_replay=True)
        for name, expected_count in (("replay_agent", 2), ("replay_tester", 2), ("evaluation", 1)):
            calls = [call for call in self.calls if call[0] == name]
            self.assertEqual(len(calls), expected_count)
            self.assertTrue(all(call[1:3] == ("fake-b", name + "-model") for call in calls))
            self.assertTrue(all("Сохраняй термины пользователя." in call[3] for call in calls))
        states = list((self.target / ".memory-runs").glob("consolidation-runs/*/personal/status.json"))
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual((state["state"], state["replay_status"], state["replay_choice"]),
                         ("published", "completed", "candidate"))
        initialization = json.loads((states[0].parent / "replay/result/initialization.json").read_text(encoding="utf-8"))
        self.assertEqual(initialization["system_prompt_source"], "configured_provider")


class HistoryTests(unittest.TestCase):
    """Проверяет исходные сообщения, измерения, родительские связи и ошибки адаптеров."""

    def test_claude_blocks_and_subagents(self):
        """Без входов проверяет tool_use/result, дерево сообщений и отдельную ветку субагента."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{"type": "assistant", "sessionId": "s", "cwd": "/work/project", "uuid": "a",
                     "message": {"content": [{"type": "thinking", "thinking": "hidden"},
                                  {"type": "tool_use", "id": "call", "name": "Read", "input": {"path": "a.py"}}]}},
                    {"type": "user", "uuid": "b", "parentUuid": "a", "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "call", "content": "Полный результат"}]}}]
            write_jsonl(root / "s.jsonl", rows)
            child = root / "s/subagents"
            child.mkdir(parents=True)
            write_jsonl(child / "agent-a.jsonl", [{"type": "user", "message": {"content": "Подзадача"}}])
            source = load_history_source({"type": "claude", "path": str(root)}, root)
            sessions = source.list_sessions()
            events = list(source.iter_events(sessions[0]))
            self.assertEqual(len(sessions), 1)
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0].data["message"]["content"][0]["type"], "tool_use")
            self.assertEqual(events[1].parent_id, "a")
            self.assertTrue(events[-1].metadata["subagent"])

    def test_codex_rollout_preserves_tools_and_usage_without_duplicate_messages(self):
        """Без входов проверяет инструменты, compaction и token_count без дублирующих event_msg."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{"type": "session_meta", "payload": {"id": "s", "cwd": "/work/project"}},
                    {"type": "response_item", "payload": {"type": "function_call", "call_id": "a", "arguments": "{}"}},
                    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "a", "output": "ok"}},
                    {"type": "event_msg", "payload": {"type": "agent_message", "message": "duplicate"}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total": 10}}},
                    {"type": "response_item", "payload": {"type": "reasoning"}},
                    {"type": "compacted", "payload": {"message": "summary"}}]
            write_jsonl(root / "rollout-a.jsonl", rows)
            source = load_history_source({"type": "codex", "path": str(root)}, root)
            events = list(source.iter_events(source.list_sessions()[0]))
            self.assertEqual(len(events), 5)
            self.assertEqual(events[1].data, rows[1])
            self.assertEqual(events[3].data, rows[4])

    def test_gigaagent_follows_only_selected_branch(self):
        """Без входов проверяет выбранную ветку, обновления и отсутствие дублей checkpoints."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            messages = [{"id": "u", "type": "human", "content": "Вопрос"}]
            history = [{"checkpoint": {"checkpoint_id": "head"}, "parent_checkpoint": {"checkpoint_id": "base"},
                        "values": {"messages": [*messages, {"id": "a", "type": "ai", "content": "Ответ"}]}},
                       {"checkpoint": {"checkpoint_id": "other"}, "parent_checkpoint": {"checkpoint_id": "base"},
                        "values": {"messages": [*messages, {"id": "wrong", "content": "Другая ветка"}]}},
                       {"checkpoint": {"checkpoint_id": "base"}, "parent_checkpoint": None,
                        "values": {"messages": messages}}]
            (root / "giga.json").write_text(json.dumps({"threads": [{"thread_id": "t", "history": history}]}), encoding="utf-8")
            source = load_history_source({"type": "gigaagent", "path": "giga.json"}, root)
            events = list(source.iter_events(source.list_sessions()[0]))
            self.assertEqual([event.data["id"] for event in events], ["u", "a"])

    def test_phoenix_groups_sessions_and_keeps_span_tree(self):
        """Без входов проверяет сессию, trace без сессии, контекст и родительский span."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{"context": {"trace_id": "t", "span_id": "root"}, "attributes": {"session.id": "s"}},
                    {"context": {"trace_id": "t", "span_id": "child"}, "parent_id": "root",
                     "attributes": {"llm.input_messages.0.message.content": "Контекст"}},
                    {"context": {"trace_id": "isolated", "span_id": "x"}, "attributes": {}}]
            write_jsonl(root / "spans.jsonl", rows)
            source = load_history_source({"type": "phoenix", "path": "spans.jsonl"}, root)
            sessions = source.list_sessions()
            self.assertEqual([session["id"] for session in sessions], ["s", "trace:isolated"])
            events = list(source.iter_events(sessions[0]))
            self.assertEqual(events[1].parent_id, "root")
            self.assertTrue(events[1].metadata["input_messages_are_context"])

    def test_generic_sqlite_filters_user_and_preserves_source(self):
        """Без входов проверяет запрос, владельца и неизменность исходной SQLite."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "logs.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE logs(id, owner, conversation, content)")
                connection.executemany("INSERT INTO logs VALUES(?,?,?,?)", [(1, "alice", "s", "a"), (2, "bob", "s", "b")])
                connection.commit()
            before = database.read_bytes()
            source = load_history_source({"type": "sqlite", "path": "logs.db", "query": "SELECT * FROM logs ORDER BY id",
                                          "session_field": "conversation", "user_field": "owner", "user_id": "alice"}, root)
            events = list(source.iter_events(source.list_sessions()[0]))
            self.assertEqual([event.data["content"] for event in events], ["a"])
            self.assertEqual(database.read_bytes(), before)

    def test_broken_jsonl_has_line_number(self):
        """Без входов проверяет явный отказ вместо пропуска повреждённой записи."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.jsonl"
            path.write_text('{}\n{"broken"', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "строка 2"):
                with read_jsonl(path) as records:
                    list(records)

    def test_phoenix_api_reads_all_pages_and_rejects_stuck_cursor(self):
        """Без входов проверяет пагинацию REST и отказ при повторяющемся курсоре."""
        source = load_history_source({"type": "phoenix", "url": "https://example.test", "project": "my project"}, Path.cwd())
        span = {"context": {"trace_id": "t", "span_id": "s"}, "attributes": {}}
        with patch("command_agent_memory.history_sources.conversation_history.request_json", side_effect=[
                {"data": [span], "next_cursor": "next"}, {"data": [], "next_cursor": None}]) as request:
            self.assertEqual(len(source.list_sessions()), 1)
            self.assertIn("my%20project", request.call_args_list[0].args[1])
            self.assertIn("cursor=next", request.call_args_list[1].args[1])
        with patch("command_agent_memory.history_sources.conversation_history.request_json", return_value={"data": [], "next_cursor": "same"}):
            with self.assertRaisesRegex(ValueError, "повторил курсор"):
                source.list_sessions()

    def test_giga_api_pagination_and_incomplete_branch(self):
        """Без входов проверяет POST истории, продвижение before и неполную цепочку."""
        source = load_history_source({"type": "gigaagent", "url": "https://example.test/api", "thread_ids": ["a/b"]}, Path.cwd())
        head = {"checkpoint": {"checkpoint_id": "a"}, "parent_checkpoint": {"checkpoint_id": "missing"},
                "values": {"messages": [{"id": "u", "content": "Вопрос"}]}}
        with patch("command_agent_memory.history_sources.conversation_history.request_json", side_effect=[[head], []]) as request:
            with self.assertRaisesRegex(ValueError, "Неполная"):
                list(source.iter_events(source.list_sessions()[0]))
            self.assertEqual(request.call_args_list[0].args[1], "threads/a%2Fb/history")
            self.assertEqual(request.call_args_list[1].kwargs["body"]["before"], head["checkpoint"])


class StoreTests(unittest.TestCase):
    """Проверяет ограничение записи выбранными путями и восстановление при сбое."""

    def test_paths_cannot_escape_or_overlap(self):
        """Без входов проверяет недопустимые пути и пересечение областей памяти."""
        for name in ("../a", "/a", "C:/a", "a\\b", "."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                MemoryLayout((name,))
        with self.assertRaises(ValueError):
            MemoryLayout(("notes",), ("notes/a.md",))

    def test_failed_publish_rolls_back_files(self):
        """Без входов имитирует сбой второй записи; проверяет оригиналы и резервную копию."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.md").write_bytes(b"old-a")
            (root / "b.md").write_bytes(b"old-b")
            store = FileMemoryStore(root, ("a.md", "b.md"))
            before = store.snapshot()
            original = store._write

            def fail_once(name, content):
                """Принимает запись; имитирует один сбой b.md, позволяя откат остальных файлов."""
                if name == "b.md" and content == b"new-b":
                    raise OSError("Имитированный сбой")
                original(name, content)

            with patch.object(store, "_write", side_effect=fail_once), self.assertRaises(OSError):
                store.publish(before, {"a.md": b"new-a", "b.md": b"new-b"}, root / "run")
            self.assertEqual(store.snapshot(), before)
            self.assertEqual((root / "run/external-backup/a.md").read_bytes(), b"old-a")


if __name__ == "__main__":
    unittest.main()
