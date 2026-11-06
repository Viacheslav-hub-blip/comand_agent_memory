"""Проверки реестра командных знаний без сетевых вызовов.

Функция update создаёт решение по знанию. RegistryTests проверяет источники,
версии и переходы состояний; RegistryPublicationTests проверяет публикацию,
доставку, восстановление после ошибки и повторное рассмотрение на новых данных.
Методы setUp/publish/runner/source готовят входы и запускают локальный конвейер.
Проверки RegistryTests: test_pending_is_not_active_and_old_knowledge_is_preserved,
test_new_evidence_can_promote_pending_and_keeps_history,
test_conflicts_remove_both_sides_but_keep_other_knowledge,
test_invalid_sources_paths_and_shared_documents_are_rejected,
test_legacy_migration_preserves_bytes_without_inventing_evidence,
test_replay_does_not_create_independent_source,
test_original_binary_document_can_be_imported_by_source_id.
Проверки RegistryPublicationTests:
test_pending_is_saved_but_not_delivered_then_new_chat_promotes_it,
test_invalid_or_rejected_update_keeps_both_memory_and_registry,
test_conflict_is_removed_from_previously_delivered_memory,
test_failed_file_write_rolls_back_registry_with_documents.
Вложенная fail_document имитирует ошибку диска. Все тесты возвращают None.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.memory_storage.team_registry import (
    REGISTRY_PATH, active_documents, apply_updates, load_registry, registry_snapshot, source_evidence,
)
from command_agent_memory.memory_update.team_memory_update import consolidate_team_memory, parse_team_decision
from command_agent_memory.team_memory_delivery.local_copy import sync_team_memory


def update(identifier: str, status: str, evidence_ids: list[str], *, conflicts=(), text="Проверенное знание") -> dict:
    """Принимает ID, состояние, источники и текст; возвращает полное решение LLM для теста."""
    return {"id": identifier, "status": status, "statement": text, "conditions": "Проект и версия определения",
            "reason": "Основание сопоставлено с исходными сведениями", "evidence_ids": evidence_ids,
            "conflicts_with": list(conflicts), "documents": {f"wiki/{identifier}.md": text}}


class RegistryTests(unittest.TestCase):
    """Проверяет контракт реестра на данных в памяти без вызова модели и публикации."""

    def setUp(self):
        """Без входов создаёт пустой реестр и два источника; возвращает None."""
        self.registry = load_registry({})
        self.evidence = {"source-a": {"acceptances": [{"run": "a"}]},
                         "source-b": {"acceptances": [{"run": "b"}]}}

    def test_wrapped_team_decision_preserves_outer_document(self):
        """Без входов проверяет Markdown, вложенный JSON документа и отказ неверной схеме."""
        inner = {"decision": "needs_resolution", "summary": "Пример внутри документа",
                 "findings": [], "updates": []}
        decision = {"decision": "accepted", "summary": "Готово", "findings": [],
                    "updates": [update("example", "accepted", ["source-a"], text=json.dumps(inner))]}
        reply = "Результат:\n```json\n" + json.dumps(decision) + "\n```"
        self.assertEqual(parse_team_decision(reply), decision)
        for invalid in ({**decision, "extra": True}, {**decision, "updates": {}}, {"choice": "candidate"}):
            with self.assertRaises(ValueError):
                parse_team_decision(json.dumps(invalid))

    def test_pending_is_not_active_and_old_knowledge_is_preserved(self):
        """Без входов проверяет сохранение принятого и изоляцию кандидата; возвращает None."""
        result = apply_updates(self.registry, [update("known", "accepted", ["source-a"]),
                                              update("candidate", "pending", ["source-b"])], self.evidence, "run-1")
        active = active_documents(result)
        self.assertIn("wiki/known.md", active)
        self.assertNotIn("wiki/candidate.md", active)
        self.assertNotIn(b"candidate", active["INDEX.md"])
        self.assertEqual(result["entries"]["candidate"]["versions"][-1]["status"], "pending")
        unchanged = apply_updates(result, [], {}, "run-2")
        self.assertEqual(unchanged, result)

    def test_new_evidence_can_promote_pending_and_keeps_history(self):
        """Без входов принимает кандидата по новому основанию и проверяет историю; возвращает None."""
        first = apply_updates(self.registry, [update("metric", "pending", ["source-a"])], self.evidence, "run-1")
        with self.assertRaisesRegex(ValueError, "новое основание"):
            apply_updates(first, [update("metric", "accepted", ["source-a"])], {}, "repeat")
        second = apply_updates(first, [update("metric", "accepted", ["source-a", "source-b"])], {}, "run-2")
        versions = second["entries"]["metric"]["versions"]
        self.assertEqual([version["status"] for version in versions], ["pending", "accepted"])
        self.assertEqual([version["revision"] for version in versions], [1, 2])
        self.assertIn("wiki/metric.md", active_documents(second))
        repeated = apply_updates(second, [update("metric", "accepted", ["source-a", "source-b"])], {}, "replay")
        self.assertEqual(repeated, second)

    def test_conflicts_remove_both_sides_but_keep_other_knowledge(self):
        """Без входов исключает обе стороны спора и сохраняет независимую запись; возвращает None."""
        first = apply_updates(self.registry, [update("old", "accepted", ["source-a"]),
                                              update("unrelated", "accepted", ["source-a"])], self.evidence, "run-1")
        second = apply_updates(first, [update("old", "conflicts", ["source-a"], conflicts=["new"]),
                                       update("new", "conflicts", ["source-b"], conflicts=["old"], text="Другое утверждение")],
                               {}, "run-2")
        active = active_documents(second)
        self.assertEqual(set(active), {"INDEX.md", "wiki/unrelated.md"})
        self.assertEqual(len(second["entries"]["old"]["versions"]), 2)
        with self.assertRaisesRegex(ValueError, "взаимными"):
            apply_updates(second, [update("old", "accepted", ["source-a", "source-b"])], {}, "invalid")
        resolved = apply_updates(second, [update("old", "accepted", ["source-a", "source-b"]),
                                         update("new", "pending", ["source-b"])], {}, "resolved")
        self.assertIn("wiki/old.md", active_documents(resolved))
        self.assertNotIn("wiki/new.md", active_documents(resolved))

    def test_invalid_sources_paths_and_shared_documents_are_rejected(self):
        """Без входов передаёт неверные основания, пути и владельцев файлов; возвращает None."""
        invalid = [update("item", "accepted", ["invented"]), update("item", "conflicts", ["source-a"])]
        for name in ("../escape.md", "INDEX.md", REGISTRY_PATH):
            item = update("item", "accepted", ["source-a"])
            item["documents"] = {name: "Текст"}
            invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                apply_updates(self.registry, [item], self.evidence, "bad")
        left, right = update("left", "accepted", ["source-a"]), update("right", "accepted", ["source-b"])
        right["documents"] = left["documents"]
        with self.assertRaisesRegex(ValueError, "нескольким"):
            apply_updates(self.registry, [left, right], self.evidence, "bad")

    def test_legacy_migration_preserves_bytes_without_inventing_evidence(self):
        """Без входов переносит прежнюю память с бинарным приложением; возвращает None."""
        snapshot = {"INDEX.md": b"old index", "wiki/old.md": "Прежние знания".encode(), "cases/data.bin": b"\x00\xff"}
        registry = load_registry(snapshot)
        active = active_documents(registry)
        self.assertEqual(active["wiki/old.md"], snapshot["wiki/old.md"])
        self.assertEqual(active["cases/data.bin"], snapshot["cases/data.bin"])
        self.assertFalse(registry["sources"])
        self.assertTrue(all(not entry["versions"][0]["evidence_ids"] for entry in registry["entries"].values()))
        published = registry_snapshot(registry)
        self.assertEqual(load_registry(published), registry)
        published["wiki/old.md"] = b"external change"
        with self.assertRaisesRegex(RuntimeError, "не соответствует"):
            load_registry(published)

    def test_replay_does_not_create_independent_source(self):
        """Без входов сравнивает источники одного чата до и после replay; возвращает None."""
        metadata = {"user_id": "alice", "target": "alice-memory", "replay_status": "not_requested",
                    "analysis_sessions": [{"source": "codex", "session_id": "chat-1"}]}
        files = {"wiki/metric.md": b"metric"}
        first = source_evidence([(Path("run-1"), metadata, files)])
        metadata = {**metadata, "replay_status": "completed", "replay_choice": "candidate"}
        second = source_evidence([(Path("run-2"), metadata, files)])
        self.assertEqual(set(first), set(second))
        result = apply_updates(self.registry, [], first, "first")
        result = apply_updates(result, [], second, "second")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(next(iter(result["sources"].values()))["acceptances"]), 2)
        metadata["analysis_sessions"] = [{"source": "codex", "session_id": "chat-2"}]
        self.assertNotEqual(set(first), set(source_evidence([(Path("run-3"), metadata, files)])))

    def test_original_binary_document_can_be_imported_by_source_id(self):
        """Без входов импортирует бинарные байты принятого документа по source-ID; возвращает None."""
        metadata = {"user_id": "alice", "target": "memory", "replay_status": "not_requested"}
        evidence = source_evidence([(Path("run"), metadata, {"cases/data.bin": b"\x00\xff"})])
        identifier = next(iter(evidence))
        item = update("attachment", "accepted", [identifier])
        item["documents"] = {"cases/data.bin": {"source_id": identifier}}
        result = apply_updates(self.registry, [item], evidence, "run-1")
        self.assertEqual(active_documents(result)["cases/data.bin"], b"\x00\xff")
        item["documents"] = {"wiki/attachment.md": "Описание приложения"}
        result = apply_updates(result, [item], evidence, "run-2")
        self.assertEqual(active_documents(result)["cases/data.bin"], b"\x00\xff")
        item["documents"] = {"cases/data.bin": "Подмена бинарного документа"}
        with self.assertRaisesRegex(ValueError, "Бинарное приложение"):
            apply_updates(result, [item], evidence, "bad")
        result["sources"][identifier]["sha256"] = "invalid"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            active_documents(result)


class RegistryPublicationTests(unittest.TestCase):
    """Проверяет файловую публикацию и доставку принятой части реестра в изолированных каталогах."""

    def setUp(self):
        """Без входов создаёт личный принятый запуск и каталоги команды; возвращает None."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "shared"
        self.recipient = self.root / "bob"
        self.recipient.mkdir()
        (self.recipient / "AGENTS.md").write_bytes(b"personal rules")
        self.accepted = [self.source("chat-1", "Первое основание")]
        self.action = "pending"
        self.runs = []

    def source(self, session_id: str, text: str) -> Path:
        """Принимает ID сессии и текст; возвращает путь неизменного принятого личного снимка."""
        run = self.root / session_id
        files = {"knowledge/wiki/source.md": text.encode("utf-8")}
        write_snapshot(run / "after", files)
        status = {"stage": "personal", "state": "published", "user_id": "alice",
                  "target": str(self.root / "alice"), "replay_status": "not_requested",
                  "memory_layout": {"personal": [], "knowledge": ["knowledge"]},
                  "analysis_sessions": [{"source": "jsonl", "session_id": session_id}],
                  "snapshot_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        (run / "status.json").write_text(json.dumps(status), encoding="utf-8")
        return run

    def runner(self, executable, model, prompt, cwd, prefix, writable=False):
        """Принимает параметры Runner; возвращает контролируемое решение без записи моделью."""
        self.assertFalse(writable)
        sources = json.loads((prefix.parent / "sources.json").read_text(encoding="utf-8"))
        decision = {"decision": "accepted", "summary": "Сопоставление завершено", "findings": [],
                    "updates": [update("metric", self.action, list(sources))]}
        if self.action == "invalid":
            decision["updates"][0]["evidence_ids"] = ["invented"]
            decision["updates"][0]["status"] = "accepted"
        if self.action == "reject":
            decision.update(decision="needs_resolution", updates=[], findings=[
                {"file": "wiki/metric.md", "problem": "Неясность", "change": "Нужно уточнение"}])
        if self.action == "conflicts":
            decision["updates"] = [update("metric", "conflicts", list(sources), conflicts=["alternative"]),
                                    update("alternative", "conflicts", list(sources), conflicts=["metric"], text="Иная формула")]
        return json.dumps(decision)

    def publish(self) -> dict:
        """Без входов выполняет командное обновление и возвращает статус публикации."""
        run = self.root / f"team-run-{len(self.runs)}"
        self.runs.append(run)
        return consolidate_team_memory(self.target, self.accepted, run, "", "fake", self.runner, configured=True)

    def test_pending_is_saved_but_not_delivered_then_new_chat_promotes_it(self):
        """Без входов проходит pending → новое основание → accepted и доставку; возвращает None."""
        status = self.publish()
        self.assertEqual(status["knowledge_states"]["pending"], 1)
        self.assertTrue((self.target / REGISTRY_PATH).is_file())
        sync_team_memory(self.target, self.recipient, self.root / "delivery-1")
        self.assertEqual(set(read_memory(self.recipient / "team_knowledge", "knowledge")), {"INDEX.md"})
        self.accepted.append(self.source("chat-2", "Новое уточняющее основание"))
        self.action = "accepted"
        status = self.publish()
        self.assertEqual(status["knowledge_states"]["accepted"], 1)
        sync_team_memory(self.target, self.recipient, self.root / "delivery-2")
        self.assertEqual((self.recipient / "team_knowledge/wiki/metric.md").read_text(encoding="utf-8"), "Проверенное знание")
        self.assertFalse((self.recipient / "team_knowledge/.registry").exists())
        self.assertEqual((self.recipient / "AGENTS.md").read_bytes(), b"personal rules")
        versions = load_registry(read_memory(self.target, "knowledge"))["entries"]["metric"]["versions"]
        self.assertEqual([version["status"] for version in versions], ["pending", "accepted"])

    def test_invalid_or_rejected_update_keeps_both_memory_and_registry(self):
        """Без входов проверяет сохранность полного снимка при неверном решении; возвращает None."""
        self.action = "accepted"
        self.publish()
        before = read_memory(self.target, "knowledge")
        for action in ("invalid", "reject"):
            self.action = action
            with self.subTest(action=action), self.assertRaises((ValueError, RuntimeError)):
                self.publish()
            self.assertEqual(read_memory(self.target, "knowledge"), before)

    def test_conflict_is_removed_from_previously_delivered_memory(self):
        """Без входов доставляет знание, затем снимает спорное и сохраняет бесспорное; возвращает None."""
        write_snapshot(self.target, {"wiki/unrelated.md": b"unrelated", "INDEX.md": b"old index"})
        self.action = "accepted"
        self.publish()
        sync_team_memory(self.target, self.recipient, self.root / "delivery-1")
        self.assertTrue((self.recipient / "team_knowledge/wiki/metric.md").is_file())
        self.accepted.append(self.source("chat-2", "Противоречащее утверждение"))
        self.action = "conflicts"
        self.assertEqual(self.publish()["knowledge_states"]["conflicts"], 2)
        sync_team_memory(self.target, self.recipient, self.root / "delivery-2")
        self.assertEqual(set(read_memory(self.recipient / "team_knowledge", "knowledge")),
                         {"INDEX.md", "wiki/unrelated.md"})
        self.assertEqual((self.recipient / "AGENTS.md").read_bytes(), b"personal rules")

    def test_failed_file_write_rolls_back_registry_with_documents(self):
        """Без входов вызывает сбой записи после реестра; проверяет общий откат, возвращает None."""
        self.publish()
        before = read_memory(self.target, "knowledge")
        self.accepted.append(self.source("chat-2", "Уточнение"))
        self.action = "accepted"
        from command_agent_memory.memory_storage import file_transaction
        original_write = file_transaction.install_file

        def fail_document(path, expected, content, captured):
            """Принимает запись файла; имитирует отказ документа, остальные записи выполняет штатно."""
            if path == self.target / "wiki/metric.md" and "captured" in captured.parts:
                raise OSError("Ошибка диска")
            return original_write(path, expected, content, captured)

        with patch.object(file_transaction, "install_file", fail_document), self.assertRaisesRegex(OSError, "Ошибка диска"):
            self.publish()
        self.assertEqual(read_memory(self.target, "knowledge"), before)


if __name__ == "__main__":
    unittest.main()
