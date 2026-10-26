"""Проверяет запуск анализа и уникальность каталогов консолидации без сетевых вызовов.

Вход: временная SQLite и имитация Codex. Выход: результаты unittest.
Класс ChatAnalysisTests: setUp, tearDown — окружение; simulate_codex — граница CLI;
execute — запуск скрипта; test_two_reports_per_chat — две пары отчётов;
test_failed_pass_cannot_mix_old_report — незавершённый запуск со старым файлом.
test_unique_consolidation_runs_with_same_time — одинаковое время двух запусков.
test_completed_chats_are_not_analyzed_again — повтор без запросов;
test_only_new_chat_is_analyzed — добавление одной сессии.
"""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memory_pipeline import ROOT, read_reports


class ChatAnalysisTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет отдельные вызовы и файлы каждого чата."""

    def setUp(self):
        """Без входов создаёт два диалога в SQLite; возвращает None."""
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.workspace = Path(self.temporary.name)
        with closing(sqlite3.connect(self.workspace / "chat_logs.db")) as database:
            database.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id TEXT, session_id TEXT, role TEXT, content TEXT, message_json TEXT)")
            database.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", [
                (1, "u", "first", "user", "Первый чат", json.dumps({"content": "Первый чат"})),
                (2, "u", "second", "user", "Второй чат", json.dumps({"content": "Второй чат"})),
            ])
            database.commit()
        self.calls = []
        self.fail_procedures = False

    def tearDown(self):
        """Без входов освобождает только созданную временную область; возвращает None."""
        self.temporary.cleanup()

    def simulate_codex(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает вызов CLI; сохраняет свободный текст либо вызывает сбой, возвращает строку."""
        self.assertTrue(writable)
        self.calls.append((prefix.parent.name, prefix.name))
        if self.fail_procedures and prefix.name == "procedures":
            raise RuntimeError("Ошибка второго анализа")
        path = self.workspace / "raw/chat-reports-codex" / prefix.parent.name / f"{prefix.name}.md"
        path.write_text(f"Произвольный текст {prefix.name}", encoding="utf-8")
        return "Сохранено"

    def execute(self):
        """Без входов запускает настоящий CLI-разбор аргументов с имитацией Codex; возвращает None."""
        arguments = ["analyze_chats_codex.py", "--user-id", "u", "--workspace", str(self.workspace)]
        with patch("sys.argv", arguments), patch("codex_client.find_codex", return_value="codex"):
            with patch("codex_client.run_codex", side_effect=self.simulate_codex), redirect_stdout(io.StringIO()):
                runpy.run_path(str(ROOT / "analyze_chats_codex.py"), run_name="__main__")

    def test_two_reports_per_chat(self):
        """Без входов проверяет четыре отдельных вызова и передачу двух пар целиком; возвращает None."""
        self.execute()
        self.assertEqual(self.calls, [
            ("session-first", "context"), ("session-first", "procedures"),
            ("session-second", "context"), ("session-second", "procedures"),
        ])
        context, procedures, count = read_reports(self.workspace / "raw/chat-reports-codex")
        self.assertEqual(count, 2)
        self.assertEqual(context, "Произвольный текст context\n\nПроизвольный текст context")
        self.assertEqual(procedures, "Произвольный текст procedures\n\nПроизвольный текст procedures")

    def test_failed_pass_cannot_mix_old_report(self):
        """Без входов проверяет исключение старого результата после сбоя нового прохода; возвращает None."""
        session = self.workspace / "raw/chat-reports-codex/session-first"
        session.mkdir(parents=True)
        (session / "procedures.md").write_text("Старый результат", encoding="utf-8")
        self.fail_procedures = True
        with self.assertRaises(SystemExit):
            self.execute()
        with self.assertRaisesRegex(RuntimeError, "не завершён"):
            read_reports(session.parent)

    def test_completed_chats_are_not_analyzed_again(self):
        """Проверяет отсутствие повторных вызовов для завершённых чатов; возвращает None."""
        self.execute()
        initial = list(self.calls)
        self.execute()
        self.assertEqual(self.calls, initial)

    def test_only_new_chat_is_analyzed(self):
        """Добавляет новую сессию; проверяет анализ только её истории, возвращает None."""
        self.execute()
        with closing(sqlite3.connect(self.workspace / "chat_logs.db")) as database:
            database.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                             (3, "u", "third", "user", "Третий чат", json.dumps({"content": "Третий чат"})))
            database.commit()
        self.execute()
        self.assertEqual(self.calls[4:], [("session-third", "context"), ("session-third", "procedures")])

    def test_unique_consolidation_runs_with_same_time(self):
        """Без входов проверяет разные каталоги при совпадении времени запусков; возвращает None."""
        user = self.workspace / "user"
        reports = user / "raw/chat-reports-codex/session-one"
        reports.mkdir(parents=True)
        (reports / "context.md").write_text("Контекст", encoding="utf-8")
        (reports / "procedures.md").write_text("Решение", encoding="utf-8")
        shutil.copytree(ROOT / "agents", self.workspace / "agents")
        arguments = ["consolidate_reports_codex.py", "--user-id", "u", "--workspace", str(user), "--stage", "personal"]
        with patch("memory_pipeline.ROOT", self.workspace), patch("sys.argv", arguments):
            with patch("codex_client.find_codex", return_value="codex"), patch("datetime.datetime") as clock:
                clock.now.return_value.strftime.return_value = "same-time"
                with patch("reviewed_consolidation.consolidate_with_review") as consolidate:
                    with redirect_stdout(io.StringIO()):
                        for _ in range(2):
                            runpy.run_path(str(ROOT / "consolidate_reports_codex.py"), run_name="__main__")
        paths = [call.args[6] for call in consolidate.call_args_list]
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path.parent.is_dir() for path in paths))


if __name__ == "__main__":
    unittest.main()
