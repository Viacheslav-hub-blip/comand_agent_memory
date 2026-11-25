"""Проверяет подготовку всей личной памяти до её переноса без вызовов моделей.

Вход: временная личная память и ответы процессов. Выход: результаты unittest.
Класс PersonalMemoryProposalTests: setUp, tearDown — окружение;
simulate_codex — два консолидатора и ревьюер; execute — запуск;
test_prepare_and_review_do_not_publish — разделение этапов;
test_publish_complete_personal_memory — совместный перенос;
test_knowledge_review_rejects_whole_proposal — отказ по знаниям;
test_knowledge_writer_failure_preserves_originals — сбой второго консолидатора;
test_only_knowledge_preserves_personal_rules — вход без личных правил;
test_knowledge_publication_failure_restores_all_entries — восстановление
(вложенная fail_on_knowledge); test_review_changes_are_published — исправленная копия;
test_baseline_change_is_not_published — подмена исходного снимка;
test_reports_record_writer_and_reviewer_changes — отчёты отдельных изменений.
test_knowledge_cannot_change_rules_without_custom_layout — границы стандартной области.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import command_agent_memory.memory_update.personal_memory_update as pipeline
from command_agent_memory.memory_update import memory_proposal as proposals, memory_review as reviews
from command_agent_memory.memory_update.memory_proposal import prepare_memory
from command_agent_memory.memory_update.memory_review import review_memory
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.skill_resources import ROOT


class PersonalMemoryProposalTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет объединённую память и сохранность оригиналов."""

    def setUp(self):
        """Без входов создаёт личную память и файлы задач; возвращает None."""
        temporary_root = ROOT / "tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.base = Path(self.temporary.name)
        self.target = self.base / "user"
        self.run = self.base / "run"
        self.before = {
            "AGENTS.md": b"old rules", "USER_PREFERENCES.md": b"old preferences",
            "user_llm_wiki/INDEX.md": b"old context",
            "knowledge/INDEX.md": b"old index", "knowledge/wiki/old.md": b"old knowledge",
        }
        write_snapshot(self.target, self.before)
        self.outside = {
            "chat_logs.db": b"database", "raw/report.md": b"report",
            "synthetic_team/inputs/data.csv": b"input", "skills/existing/SKILL.md": b"existing skill",
        }
        for name, content in self.outside.items():
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.skill = ROOT / "command_agent_memory/skills/personal-memory-consolidation/SKILL.md"
        self.writer_calls = []
        self.review_count = 0
        self.fail_knowledge = False
        self.reject_knowledge = False
        self.change_review_copy = False
        self.change_baseline = False
        self.cross_scope = False
        self.new_knowledge = {
            "knowledge/INDEX.md": b"new index", "knowledge/wiki/definition.md": b"definition",
            "knowledge/cases/example.md": b"case", "knowledge/skills/procedure/SKILL.md": b"procedure",
        }

    def tearDown(self):
        """Без входов освобождает созданную тестом область; возвращает None."""
        self.assertTrue(self.base.resolve().is_relative_to((ROOT / "tmp").resolve()))
        self.temporary.cleanup()

    def simulate_codex(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает параметры CLI; меняет копию или выдаёт решение ревью, возвращает текст."""
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        if prefix.name.startswith("consolidation"):
            self.writer_calls.append(prefix.name)
            if prefix.name == "consolidation":
                materials_path = self.run / "consolidation-materials.md"
                self.assertIn(materials_path.as_posix(), prompt)
                self.assertEqual(materials_path.read_text(encoding="utf-8"), "PERSONAL_INPUT")
                (cwd / "memory/AGENTS.md").write_bytes(b"new rules")
                return "Правила обновлены"
            materials_path = self.run / "consolidation-knowledge-materials.md"
            self.assertIn(materials_path.as_posix(), prompt)
            materials = materials_path.read_text(encoding="utf-8")
            self.assertIn("DOMAIN_INPUT", materials)
            if "consolidation" in self.writer_calls:
                self.assertIn("PERSONAL_INPUT", materials)
                saved = json.loads((self.run / "report/index.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["personal"]["state"], "completed")
                self.assertEqual(saved["knowledge"]["state"], "running")
            if self.fail_knowledge:
                raise RuntimeError("Сбой консолидатора знаний")
            for name, content in self.new_knowledge.items():
                path = cwd / "memory" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            if self.cross_scope:
                (cwd / "memory/AGENTS.md").write_bytes(b"knowledge changed personal rules")
            return "Знания обновлены"
        self.review_count += 1
        self.assertTrue(writable)
        if self.change_review_copy:
            (cwd / "memory/AGENTS.md").write_bytes(b"corrected by reviewer")
        if self.change_baseline:
            (self.run / "before/AGENTS.md").write_bytes(b"corrupted baseline")
        if self.reject_knowledge:
            return json.dumps({
                "decision": "changes_requested", "summary": "Ошибка в знаниях",
                "findings": [{"file": "knowledge/INDEX.md", "problem": "Ошибка", "change": "Исправить"}],
            })
        return json.dumps({"decision": "accepted", "summary": "Принято", "findings": []})

    def execute(self, personal_materials="PERSONAL_INPUT"):
        """Принимает личные материалы; выполняет весь этап с DOMAIN_INPUT, возвращает None."""
        with (
            patch.object(pipeline, "run_codex", side_effect=self.simulate_codex),
            patch.object(proposals, "run_codex", side_effect=self.simulate_codex),
            patch.object(reviews, "run_codex", side_effect=self.simulate_codex),
        ):
            pipeline.consolidate_with_review(
                self.target, "personal", personal_materials, self.skill, "codex", "test", self.run,
                knowledge_materials="DOMAIN_INPUT",
            )

    def test_prepare_and_review_do_not_publish(self):
        """Без входов проверяет отсутствие записи до отдельного переноса; возвращает None."""
        self.run.mkdir()
        with memory_lock(self.target):
            with (
                patch.object(pipeline, "run_codex", side_effect=self.simulate_codex),
                patch.object(proposals, "run_codex", side_effect=self.simulate_codex),
                patch.object(reviews, "run_codex", side_effect=self.simulate_codex),
            ):
                proposal = prepare_memory(
                    self.target, "personal", "PERSONAL_INPUT", self.skill, "codex", "test", self.run,
                    knowledge_materials="DOMAIN_INPUT",
                )
                self.assertEqual(read_memory(self.target, "personal"), self.before)
                self.assertEqual(self.review_count, 0)
                self.assertEqual(proposal.after["AGENTS.md"], b"new rules")
                self.assertEqual(proposal.after["knowledge/wiki/definition.md"], b"definition")
                self.assertFalse((self.run / "after").exists())
                proposal, decision = review_memory(proposal, "codex", "test")
        self.assertEqual(decision["decision"], "accepted")
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        materials = (self.run / "materials.md").read_text()
        self.assertIn("PERSONAL_INPUT", materials)
        self.assertIn("DOMAIN_INPUT", materials)

    def test_publish_complete_personal_memory(self):
        """Без входов проверяет перенос правил и знаний после ревью всей копии; возвращает None."""
        self.execute()
        expected = self.before | {"AGENTS.md": b"new rules"} | self.new_knowledge
        self.assertEqual(read_memory(self.target, "personal"), expected)
        self.assertEqual(self.writer_calls, ["consolidation", "consolidation-knowledge"])
        self.assertEqual(self.review_count, 1)
        for name, content in self.outside.items():
            self.assertEqual((self.target / name).read_bytes(), content)
        self.assertEqual(read_memory(self.run / "before", "personal"), self.before)

    def test_knowledge_review_rejects_whole_proposal(self):
        """Без входов проверяет сохранность всех оригиналов при отказе по знаниям; возвращает None."""
        self.reject_knowledge = True
        with self.assertRaisesRegex(RuntimeError, "Ошибка в знаниях"):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        self.assertEqual((self.run / "after/AGENTS.md").read_bytes(), b"new rules")
        self.assertEqual(json.loads((self.run / "status.json").read_text())["state"], "rejected")

    def test_knowledge_writer_failure_preserves_originals(self):
        """Без входов проверяет сохранность после сбоя второго консолидатора; возвращает None."""
        self.fail_knowledge = True
        with self.assertRaisesRegex(RuntimeError, "Сбой консолидатора знаний"):
            self.execute()
        self.assertEqual(self.writer_calls, ["consolidation", "consolidation-knowledge"])
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        self.assertEqual(self.review_count, 0)
        self.assertEqual(json.loads((self.run / "status.json").read_text())["state"], "failed")
        index = json.loads((self.run / "report/index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["personal"]["state"], "completed")
        self.assertEqual(index["knowledge"]["state"], "failed")
        self.assertEqual(index["publication"]["state"], "failed")
        self.assertIn("Правила обновлены", (self.run / "report/personal.md").read_text(encoding="utf-8"))

    def test_knowledge_cannot_change_rules_without_custom_layout(self):
        """Без входов проверяет границы стандартного layout даже для исполнителя без политики."""
        self.cross_scope = True
        with self.assertRaisesRegex(RuntimeError, "вне области своего этапа"):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        self.assertEqual(self.review_count, 0)

    def test_only_knowledge_preserves_personal_rules(self):
        """Без входов проверяет личный этап без инструкций и предпочтений; возвращает None."""
        self.execute(personal_materials="")
        self.assertEqual(self.writer_calls, ["consolidation-knowledge"])
        self.assertEqual(read_memory(self.target, "personal"), self.before | self.new_knowledge)

    def test_knowledge_publication_failure_restores_all_entries(self):
        """Без входов проверяет восстановление четырёх частей памяти при сбое; возвращает None."""
        from command_agent_memory.memory_storage import file_transaction
        replace = file_transaction.install_file

        def fail_on_knowledge(path, expected, content, captured):
            """Принимает пути; вызывает сбой переноса knowledge, остальные операции выполняет."""
            if path.is_relative_to(self.target / "knowledge") and "captured" in captured.parts:
                raise OSError("Сбой переноса знаний")
            return replace(path, expected, content, captured)

        with patch.object(file_transaction, "install_file", side_effect=fail_on_knowledge):
            with self.assertRaisesRegex(OSError, "Сбой переноса знаний"):
                self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)
        for name, content in self.outside.items():
            self.assertEqual((self.target / name).read_bytes(), content)

    def test_review_changes_are_published(self):
        """Без входов проверяет перенос правок ревьюера и сохранность предложения до них; возвращает None."""
        self.change_review_copy = True
        self.execute()
        self.assertEqual((self.target / "AGENTS.md").read_bytes(), b"corrected by reviewer")
        self.assertEqual((self.run / "after/AGENTS.md").read_bytes(), b"corrected by reviewer")
        self.assertEqual((self.run / "before-review/AGENTS.md").read_bytes(), b"new rules")
        self.assertEqual(read_memory(self.run / "before", "personal"), self.before)

    def test_reports_record_writer_and_reviewer_changes(self):
        """Без входов проверяет отдельные diff, итоги и причину отсутствия прогона; возвращает None."""
        self.change_review_copy = True
        self.execute()
        reports = self.run / "report"
        index = json.loads((reports / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(index), {"analysis", "personal", "knowledge", "review", "replay", "publication"})
        self.assertIn("+new rules", (reports / "personal.diff").read_text(encoding="utf-8"))
        review_diff = (reports / "review.diff").read_text(encoding="utf-8")
        self.assertIn("-new rules", review_diff)
        self.assertIn("+corrected by reviewer", review_diff)
        self.assertEqual(index["replay"]["state"], "not_requested")
        self.assertIn("Принятие памяти основано на ревью", (reports / "summary.md").read_text(encoding="utf-8"))

    def test_baseline_change_is_not_published(self):
        """Без входов проверяет остановку при изменении исходного снимка; возвращает None."""
        self.change_baseline = True
        with self.assertRaisesRegex(RuntimeError, "Исходные снимки"):
            self.execute()
        self.assertEqual(read_memory(self.target, "personal"), self.before)


if __name__ == "__main__":
    unittest.main()
