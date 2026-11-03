"""Проверяет предотвращение записи непринятой памяти без вызова LLM.

Вход: временные файлы и подменённые ответы процессов. Выход: результаты unittest.
Класс ReviewedConsolidationTests содержит методы:
setUp — подготовка; tearDown — очистка; simulate_codex — имитация CLI;
execute — запуск; test_publish_after_review — принятие;
test_rejection_does_not_write_originals — замечания;
test_needs_resolution_does_not_write_originals — неразрешённый вопрос;
test_invalid_reply_does_not_publish — повреждённый ответ;
test_accepted_with_findings_does_not_publish — противоречивый ответ;
test_writer_failure_does_not_publish — сбой консолидатора;
test_reviewer_failure_does_not_publish — сбой ревьюера;
test_review_reply_with_prefix_is_parsed — ответ с пояснением перед JSON;
test_external_edit_blocks_stale_publication — внешнее изменение;
test_personal_preserves_chat_database_and_other_files — границы личной памяти;
test_publication_exception_restores_originals — восстановление (внутри fail_once);
test_second_writer_is_blocked — блокировка; test_symbolic_link_is_not_copied — symlink;
test_junction_is_not_copied — NTFS junction; test_overlapping_run_is_rejected — пересечение путей.
test_personal_publication_exception_restores_all_files — восстановление нескольких файлов
(внутри fail_on_preferences); test_accepted_deletion_is_published — удаление;
test_unchanged_memory_is_not_replaced — отсутствие лишней записи.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import command_agent_memory.memory_update.personal_memory_update as pipeline
from command_agent_memory.memory_update import memory_proposal as proposals, memory_review as reviews
from command_agent_memory.memory_storage import file_storage
from command_agent_memory.memory_storage.file_storage import publish_memory
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.skill_resources import ROOT


class ReviewedConsolidationTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет файлы и состояния, возвращает результаты тестов."""

    def setUp(self):
        """Без входов создаёт изолированную память внутри проекта; возвращает None."""
        temporary_root = ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.base = Path(self.temporary.name)
        self.target = self.base / "knowledge"
        self.before = {"INDEX.md": b"old index", "wiki/page.md": b"old page"}
        write_snapshot(self.target, self.before)
        self.after = {"INDEX.md": b"new index", "wiki/page.md": b"new page"}
        self.stage = "knowledge"
        self.decision = {"decision": "accepted", "summary": "Принято", "findings": []}
        self.raw_reply = None
        self.failure = None
        self.change_original = False
        self.review_count = 0

    def tearDown(self):
        """Без входов удаляет созданную тестом временную область; возвращает None."""
        self.assertTrue(self.base.resolve().is_relative_to((ROOT / "tmp").resolve()))
        self.temporary.cleanup()

    def simulate_codex(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает аргументы CLI; имитирует изменение копии или ответ, возвращает текст."""
        if self.failure == "writer" and prefix.name.startswith("consolidation"):
            raise RuntimeError("Сбой консолидатора")
        if prefix.name.startswith("consolidation"):
            for path in (cwd / "memory").rglob("*"):
                if path.is_file():
                    path.unlink()
            for name, content in self.after.items():
                path = cwd / "memory" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            return "Копия изменена"
        self.review_count += 1
        self.assertTrue(writable)
        if self.failure == "review":
            raise RuntimeError("Сбой ревьюера")
        self.assertEqual(read_memory(self.target, self.stage), self.before)
        if self.change_original:
            (self.target / "external.md").write_bytes(b"external edit")
        return self.raw_reply if self.raw_reply is not None else json.dumps(self.decision)

    def execute(self):
        """Без входов запускает обработку с имитацией CLI; возвращает None или исключение."""
        skill = ROOT / "command_agent_memory/skills/knowledge-consolidation/SKILL.md"
        with (
            patch.object(pipeline, "run_codex", side_effect=self.simulate_codex),
            patch.object(proposals, "run_codex", side_effect=self.simulate_codex),
            patch.object(reviews, "run_codex", side_effect=self.simulate_codex),
        ):
            pipeline.consolidate_with_review(
                self.target, self.stage, "Сведения", skill, "codex", "test-model", self.base / "run",
            )

    def test_publish_after_review(self):
        """Без входов проверяет перенос после одного ревью всей копии; возвращает None."""
        self.execute()
        self.assertEqual(self.review_count, 1)
        self.assertEqual(read_memory(self.target, self.stage), self.after)
        self.assertEqual(read_memory(self.base / "run/before", self.stage), self.before)
        self.assertEqual(json.loads((self.base / "run/status.json").read_text())["state"], "published")

    def test_rejection_does_not_write_originals(self):
        """Без входов проверяет сохранность оригиналов при замечаниях; возвращает None."""
        self.decision["decision"] = "changes_requested"
        self.decision["findings"] = [{"file": "wiki/page.md", "problem": "Ошибка", "change": "Исправить"}]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)
        self.assertEqual(read_memory(self.base / "run/after", self.stage), self.after)
        self.assertEqual(json.loads((self.base / "run/status.json").read_text())["state"], "rejected")

    def test_needs_resolution_does_not_write_originals(self):
        """Без входов проверяет остановку при неразрешённом вопросе; возвращает None."""
        self.decision["decision"] = "needs_resolution"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_invalid_reply_does_not_publish(self):
        """Без входов проверяет остановку на неразбираемом ответе; возвращает None."""
        self.raw_reply = "не JSON"
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_review_reply_with_prefix_is_parsed(self):
        """Без входов проверяет извлечение последнего строгого JSON из ответа с пояснением; возвращает None."""
        self.raw_reply = "Ревью завершено.\n" + json.dumps(self.decision, ensure_ascii=False)
        self.execute()
        decision = json.loads((self.base / "run/reviews/review-001.json").read_text(encoding="utf-8"))
        self.assertEqual(decision, self.decision)
        self.assertEqual(read_memory(self.target, self.stage), self.after)

    def test_accepted_with_findings_does_not_publish(self):
        """Без входов проверяет противоречивое решение accepted с замечаниями; возвращает None."""
        self.decision["findings"] = [{
            "file": "wiki/page.md", "problem": "Осталась ошибка", "change": "Исправить",
        }]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_writer_failure_does_not_publish(self):
        """Без входов проверяет сохранность памяти при сбое записи копии; возвращает None."""
        self.failure = "writer"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_reviewer_failure_does_not_publish(self):
        """Без входов проверяет сохранность памяти при сбое проверки; возвращает None."""
        self.failure = "review"
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_external_edit_blocks_stale_publication(self):
        """Без входов проверяет сохранение внешней правки вместо устаревшей копии; возвращает None."""
        self.change_original = True
        self.raw_reply = json.dumps({"decision": "changes_requested", "summary": "Стоп", "findings": []})
        with self.assertRaises(RuntimeError):
            self.execute()
        with self.assertRaisesRegex(RuntimeError, "изменилась"):
            publish_memory(self.target, self.before, self.after, self.stage, self.base / "publish-check")
        self.assertEqual((self.target / "external.md").read_bytes(), b"external edit")

    def test_personal_preserves_chat_database_and_other_files(self):
        """Без входов проверяет границу личной памяти; возвращает None."""
        self.stage = "personal"
        self.before = {"AGENTS.md": b"old rules", "USER_PREFERENCES.md": b"old preferences"}
        self.after = {"AGENTS.md": b"new rules", "USER_PREFERENCES.md": b"new preferences", "user_llm_wiki/INDEX.md": b"new wiki"}
        self.target = self.base / "personal"
        write_snapshot(self.target, self.before)
        (self.target / "chat_logs.db").write_bytes(b"database must stay")
        (self.target / "raw").mkdir()
        (self.target / "raw/report.md").write_bytes(b"report must stay")
        self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.after)
        self.assertEqual((self.target / "chat_logs.db").read_bytes(), b"database must stay")
        self.assertEqual((self.target / "raw/report.md").read_bytes(), b"report must stay")

    def test_publication_exception_restores_originals(self):
        """Без входов имитирует сбой переноса и проверяет восстановление; возвращает None."""
        calls = []
        replace = os.replace

        def fail_once(source, destination):
            """Принимает пути переноса; один раз вызывает OSError, иначе возвращает результат os.replace."""
            calls.append((source, destination))
            if len(calls) == 2:
                raise OSError("Сбой переноса")
            return replace(source, destination)

        with patch.object(file_storage.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                publish_memory(self.target, self.before, self.after, self.stage, self.base / "publication")
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_personal_publication_exception_restores_all_files(self):
        """Без входов проверяет восстановление нескольких личных файлов после сбоя; возвращает None."""
        target = self.base / "personal"
        before = {"AGENTS.md": b"old rules", "USER_PREFERENCES.md": b"old preferences"}
        after = {"AGENTS.md": b"new rules", "USER_PREFERENCES.md": b"new preferences"}
        write_snapshot(target, before)
        run = self.base / "personal-publication"
        replace = os.replace

        def fail_on_preferences(source, destination):
            """Принимает пути; вызывает сбой второй записи, иначе возвращает результат os.replace."""
            if Path(source) == run / "publish/USER_PREFERENCES.md":
                raise OSError("Сбой переноса предпочтений")
            return replace(source, destination)

        with patch.object(file_storage.os, "replace", side_effect=fail_on_preferences):
            with self.assertRaises(OSError):
                publish_memory(target, before, after, "personal", run)
        self.assertEqual(read_memory(target, "personal"), before)

    def test_accepted_deletion_is_published(self):
        """Без входов проверяет принятую моделью операцию удаления и сохранность снимка; возвращает None."""
        self.after = {"INDEX.md": b"new index"}
        self.execute()
        self.assertEqual(read_memory(self.target, self.stage), self.after)
        self.assertEqual(read_memory(self.base / "run/before", self.stage), self.before)

    def test_unchanged_memory_is_not_replaced(self):
        """Без входов проверяет ревью без повторной записи неизменной памяти; возвращает None."""
        self.after = dict(self.before)
        with patch.object(pipeline, "publish_memory") as publication:
            self.execute()
        publication.assert_not_called()
        self.assertEqual(self.review_count, 1)
        self.assertEqual(json.loads((self.base / "run/status.json").read_text())["state"], "unchanged")

    def test_second_writer_is_blocked(self):
        """Без входов проверяет исключение конкурирующего писателя; возвращает None."""
        with memory_lock(self.target):
            with self.assertRaises(RuntimeError):
                with memory_lock(self.target):
                    self.fail("Вторая блокировка не должна быть получена")

    def test_overlapping_run_is_rejected(self):
        """Без входов проверяет запрет создавать запуск внутри памяти; возвращает None."""
        with self.assertRaisesRegex(RuntimeError, "раздельными"):
            pipeline.consolidate_with_review(
                self.target, self.stage, "Сведения", Path("unused"), "codex", "test", self.target / "run",
            )
        self.assertFalse((self.target / "run").exists())
        self.assertEqual(read_memory(self.target, self.stage), self.before)

    def test_symbolic_link_is_not_copied(self):
        """Без входов проверяет отказ от перехода по ссылке; возвращает None."""
        link = self.target / "linked"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("Создание symlink недоступно в этом окружении")
        with self.assertRaisesRegex(RuntimeError, "Ссылка"):
            read_memory(self.target, self.stage)

    @unittest.skipUnless(os.name == "nt", "Проверка NTFS junction выполняется на Windows")
    def test_junction_is_not_copied(self):
        """Без входов создаёт junction в тестовой области и проверяет отказ; возвращает None."""
        link = self.target / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(self.base)],
            capture_output=True,
        )
        if result.returncode:
            self.skipTest("Создание junction недоступно")
        with self.assertRaisesRegex(RuntimeError, "Ссылка"):
            read_memory(self.target, self.stage)
        with self.assertRaisesRegex(RuntimeError, "Ссылка"):
            read_memory(link, "personal")


if __name__ == "__main__":
    unittest.main()
