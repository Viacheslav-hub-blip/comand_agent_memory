"""Проверяет передачу отчётов целиком без анализа их Markdown-содержания.

Вход: временные пары файлов и служебные состояния. Выход: результаты unittest.
Класс ReportInputTests: setUp и tearDown — окружение; test_arbitrary_text —
произвольное оформление; test_pair_replaces_legacy — выбор пары;
test_legacy_is_complete — полный старый отчёт; test_incomplete_run — сбой запуска;
test_multiple_sessions — несколько пар; test_missing_file — ошибка чтения.
test_other_user_is_rejected — запрет передачи чужих отчётов в личный этап.
"""
import json
from pathlib import Path
import tempfile
import unittest

from memory_pipeline import read_reports


class ReportInputTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет входные файлы без вызова модели."""

    def setUp(self):
        """Без входов создаёт временную пару отчётов; возвращает None."""
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.session = self.directory / "session-1"
        self.session.mkdir()
        self.context = "Любое название\n\nПредпочитает SQL. Определение без заголовка."
        self.procedures = "# Задача\nПроверка и её результат.\n```необязательное оформление"
        (self.session / "context.md").write_text(self.context, encoding="utf-8")
        (self.session / "procedures.md").write_text(self.procedures, encoding="utf-8")

    def tearDown(self):
        """Без входов удаляет только свою временную область; возвращает None."""
        self.temporary.cleanup()

    def test_arbitrary_text(self):
        """Без входов проверяет передачу произвольного текста без потерь; возвращает None."""
        self.assertEqual(read_reports(self.directory), (self.context, self.procedures, 1))

    def test_pair_replaces_legacy(self):
        """Без входов проверяет однократную передачу новой пары вместо старого файла; возвращает None."""
        (self.directory / "session-1.md").write_text("старый результат", encoding="utf-8")
        self.assertEqual(read_reports(self.directory), (self.context, self.procedures, 1))

    def test_legacy_is_complete(self):
        """Без входов проверяет передачу старого отчёта целиком; возвращает None."""
        old = "Старый отчёт с неизвестными разделами\n## Другой заголовок\nСодержание"
        (self.directory / "session-2.md").write_text(old, encoding="utf-8")
        context, procedures, count = read_reports(self.directory)
        self.assertEqual(context, self.context + "\n\n" + old)
        self.assertEqual(procedures, self.procedures)
        self.assertEqual(count, 2)

    def test_incomplete_run(self):
        """Без входов проверяет исключение результатов незавершённого запуска; возвращает None."""
        (self.session / "manifest.json").write_text(json.dumps({"state": "failed"}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "не завершён"):
            read_reports(self.directory)

    def test_multiple_sessions(self):
        """Без входов проверяет чтение двух диалогов без смешения видов отчётов; возвращает None."""
        second = self.directory / "session-2"
        second.mkdir()
        (second / "context.md").write_text("второй контекст", encoding="utf-8")
        (second / "procedures.md").write_text("второе решение", encoding="utf-8")
        self.assertEqual(read_reports(self.directory), (
            self.context + "\n\nвторой контекст", self.procedures + "\n\nвторое решение", 2,
        ))

    def test_missing_file(self):
        """Без входов проверяет ошибку чтения отсутствующего файла пары; возвращает None."""
        (self.session / "procedures.md").unlink()
        with self.assertRaises(FileNotFoundError):
            read_reports(self.directory)

    def test_other_user_is_rejected(self):
        """Передаёт чужой manifest; проверяет изоляцию личного входа, возвращает None."""
        (self.session / "manifest.json").write_text(json.dumps({
            "state": "completed", "user_id": "another-user",
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "другому пользователю"):
            read_reports(self.directory, user_id="owner")


if __name__ == "__main__":
    unittest.main()
