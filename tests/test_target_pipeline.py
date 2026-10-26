"""Проверяет целевой цикл на двух пользователях без сетевых вызовов.

Класс TargetPipelineTests: setUp/tearDown — изолированный проект;
chat — имитация двух анализаторов; agent — консолидация, review и команда;
execute — запуск конвейера; accepted_runs — поиск личных результатов.
Методы test_* проверяют передачу принятых знаний, отсутствие повторных вызовов,
отказ review, нехватку данных, защиту снимка, пустой экспорт и входные границы.
"""
from contextlib import ExitStack, closing, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import memory_pipeline
import replay_evaluation as replay
import reviewed_consolidation as personal
from command_agent_memory import pipeline
from command_agent_memory.adapters.base import ModelAdapter
from command_agent_memory.analysis import pipeline as analysis
from command_agent_memory.memory import team


class TargetPipelineTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет реальные файловые переходы с имитацией LLM."""

    def setUp(self):
        """Создаёт временный проект, две базы и подмены исполнителей; возвращает None."""
        original = memory_pipeline.ROOT
        (original / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=original / "tmp")
        self.root = Path(self.temporary.name)
        shutil.copytree(original / "agents", self.root / "agents")
        self.stack = ExitStack()
        for module in (memory_pipeline, replay, personal, pipeline, analysis, team):
            self.stack.enter_context(patch.object(module, "ROOT", self.root))
        self.stack.enter_context(patch.object(
            personal, "KNOWLEDGE_SKILL",
            self.root / "agents/consolidate-knowledge/skills/consolidate-knowledge/SKILL.md"))
        self.calls = []
        self.reject_review = False
        self.reject_team = False
        self.no_knowledge = False
        self.users = []
        for identity in ("alice", "bob"):
            workspace = self.root / identity
            workspace.mkdir()
            with closing(sqlite3.connect(workspace / "chat_logs.db")) as connection:
                connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id, session_id, role, content, message_json)")
                text = f"Задача пользователя {identity}"
                connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                                   (1, identity, "one", "user", text, json.dumps({"content": text})))
                connection.commit()
            self.users.append({"user_id": identity, "workspace": identity, "source": "sqlite"})
        self.config = self.root / "users.json"
        self.config.write_text(json.dumps(self.users), encoding="utf-8")
        adapter = ModelAdapter("openrouter", "", self.agent, self.chat)
        self.stack.enter_context(patch("command_agent_memory.adapters.load_model_adapter", return_value=adapter))
        self.stack.enter_context(patch.object(analysis, "load_model_adapter", return_value=adapter))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Сеть запрещена в тестах")))

    def tearDown(self):
        """Освобождает подмены и собственный временный проект; возвращает None."""
        self.stack.close()
        self.temporary.cleanup()

    def chat(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает запрос анализатора; возвращает текст с идентификатором пользователя."""
        self.assertFalse(writable)
        identity = "alice" if "alice" in prompt else "bob"
        self.calls.append((identity, prefix.name))
        return f"{prefix.name}: сведения {identity}"

    def agent(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает вызов агента; изменяет только копию и возвращает протокольный ответ."""
        self.assertTrue(writable)
        draft = cwd / "memory"
        if prefix.parent.name == "knowledge":
            self.calls.append(("team", "merge"))
            materials = (prefix.parent / "materials.md").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_", materials)
            self.assertIn("case for alice", materials)
            self.assertIn("case for bob", materials)
            (draft / "INDEX.md").write_text("shared index", encoding="utf-8")
            (draft / "cases").mkdir(exist_ok=True)
            (draft / "cases/shared.md").write_text("merged cases", encoding="utf-8")
            return json.dumps({"decision": "needs_resolution" if self.reject_team else "accepted",
                               "summary": "Командное решение", "findings": []})
        if prefix.name.startswith("consolidation"):
            materials = (prefix.parent / "materials.md").read_text(encoding="utf-8")
            identity = "alice" if "alice" in materials else "bob"
            self.calls.append((identity, prefix.name))
            if prefix.name == "consolidation":
                (draft / "AGENTS.md").write_text(f"PRIVATE_{identity}", encoding="utf-8")
                (draft / "USER_PREFERENCES.md").write_text(f"PRIVATE_PREF_{identity}", encoding="utf-8")
            elif not self.no_knowledge:
                knowledge = draft / "knowledge"
                (knowledge / "cases").mkdir(parents=True, exist_ok=True)
                (knowledge / "INDEX.md").write_text(f"index {identity}", encoding="utf-8")
                (knowledge / f"cases/{identity}.md").write_text(f"case for {identity}", encoding="utf-8")
            return "Копия обновлена"
        self.calls.append(("personal", "review"))
        return json.dumps({"decision": "changes_requested" if self.reject_review else "accepted",
                           "summary": "Личное решение", "findings": []})

    def execute(self, *extra):
        """Принимает дополнительные флаги; запускает настоящий конвейер с имитацией модели."""
        with redirect_stdout(io.StringIO()):
            pipeline.main(["--users", str(self.config), "--model", "test", *extra])

    def accepted_runs(self):
        """Без аргументов возвращает каталоги личных запусков текущего теста."""
        return sorted(path.parent for path in (self.root / "data/consolidation-runs").glob("*/personal/status.json"))

    def test_two_users_export_only_accepted_knowledge(self):
        """Проверяет два личных цикла, одно объединение и подключение общей памяти; возвращает None."""
        self.execute()
        self.assertEqual(len(self.calls), 11)
        self.assertEqual(self.calls.count(("team", "merge")), 1)
        self.assertEqual(self.calls.count(("personal", "review")), 2)
        for identity in ("alice", "bob"):
            self.assertEqual((self.root / identity / "AGENTS.md").read_text(), f"PRIVATE_{identity}")
            self.assertEqual((self.root / identity / "team_knowledge/INDEX.md").read_text(), "shared index")
        for run in self.accepted_runs():
            metadata, files = team.read_accepted_knowledge(run)
            self.assertEqual(metadata["replay_status"], "not_requested")
            self.assertNotIn("AGENTS.md", files)
        team_run = next((self.root / "data/consolidation-runs").glob("*/knowledge/status.json")).parent
        self.assertFalse((team_run / "reviews").exists())
        self.assertFalse((self.root / "team-memory").exists())

    def test_repeat_without_new_sessions_does_not_call_models(self):
        """Проверяет повторный запуск без новых чатов; возвращает None."""
        self.execute()
        previous = list(self.calls)
        self.execute()
        self.assertEqual(self.calls, previous)

    def test_validation_creates_no_artifacts(self):
        """Проверяет локальный validate-only без каталогов отчётов и вызовов модели; возвращает None."""
        self.execute("--validate-only")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "data").exists())
        self.assertFalse((self.root / "alice/raw").exists())

    def test_review_rejection_stops_team_input(self):
        """Проверяет сохранность оригинала и запрет командного входа при отказе review; возвращает None."""
        self.reject_review = True
        with self.assertRaisesRegex(RuntimeError, "Память не обновлена"):
            self.execute()
        self.assertNotIn(("team", "merge"), self.calls)
        self.assertFalse((self.root / "alice/AGENTS.md").exists())
        with self.assertRaisesRegex(ValueError, "не была принята"):
            team.read_accepted_knowledge(self.accepted_runs()[0])

    def test_missing_replay_data_is_recorded_as_skip(self):
        """Проверяет принятие по review без сценария вместо ложного успешного прогона; возвращает None."""
        self.execute("--stage", "personal", "--evaluate-by-replay")
        for run in self.accepted_runs():
            metadata, _ = team.read_accepted_knowledge(run)
            self.assertEqual(metadata["replay_status"], "skipped")
            self.assertNotIn("replay_choice", metadata)

    def test_changed_snapshot_cannot_enter_team_memory(self):
        """Проверяет хеш принятого источника перед командным объединением; возвращает None."""
        self.execute("--stage", "personal")
        run = self.accepted_runs()[0]
        (run / "after/knowledge/INDEX.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "снимок изменён"):
            team.read_accepted_knowledge(run)

    def test_team_rejection_preserves_existing_team_memory(self):
        """Проверяет, что отказ единственного консолидатора не публикует его копию; возвращает None."""
        self.reject_team = True
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        (knowledge / "INDEX.md").write_text("old team", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "не принято"):
            self.execute()
        self.assertEqual((knowledge / "INDEX.md").read_text(), "old team")
        self.assertFalse((self.root / "alice/team_knowledge").exists())

    def test_overlapping_user_workspaces_fail_before_models(self):
        """Проверяет запрет вложенных областей разных пользователей; возвращает None."""
        self.users[1]["workspace"] = "alice/nested"
        with self.assertRaisesRegex(ValueError, "не должны пересекаться"):
            pipeline.load_users(self.users)
        self.assertEqual(self.calls, [])

    def test_no_transferable_knowledge_does_not_call_team_model(self):
        """Проверяет личную память без командных знаний; возвращает None."""
        self.no_knowledge = True
        self.execute()
        self.assertNotIn(("team", "merge"), self.calls)
        self.assertFalse((self.root / "knowledge").exists())
        for run in self.accepted_runs():
            self.assertEqual(team.read_accepted_knowledge(run)[1], {})


if __name__ == "__main__":
    unittest.main()
