"""Регрессионные проверки файловой публикации без моделей.

Функции и методы:
setUp;
tearDown;
crash;
test_crash_after_first_file_restores_before_read;
test_crash_after_capture_restores_missing_file;
test_crash_after_new_document_removes_uncommitted_addition;
test_crash_after_deletion_restores_removed_document;
test_crash_during_rollback_can_recover_again;
test_committed_publication_survives_crash;
test_validate_only_does_not_recover_or_write;
test_rollback_preserves_concurrent_writer;
test_atomic_install_does_not_replace_file_created_during_race;
test_external_change_after_crash_is_preserved;
test_reader_cannot_observe_partial_publication;
test_read_session_blocks_publication_for_whole_task;
test_two_external_versions_are_retained_for_resolution;
test_missing_hardlink_support_keeps_originals;
change_after_first;
create_before_link;
inspect_reader;
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.memory_storage import file_transaction as transaction


class FileTransactionTests(unittest.TestCase):
    """Без сети проверяет реальные файловые операции и остановку отдельного процесса."""

    def setUp(self):
        """Без входов создаёт временную память из двух файлов; возвращает None."""
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "memory"
        self.root.mkdir()
        self.before = {"a.md": b"old-a", "b.md": b"old-b"}
        self.after = {"a.md": b"new-a", "b.md": b"new-b"}
        for name, content in self.before.items():
            (self.root / name).write_bytes(content)
        self.store = FileMemoryStore(self.root, ("a.md", "b.md"))

    def tearDown(self):
        """Без входов удаляет временные данные; возвращает None."""
        self.temporary.cleanup()

    def crash(self, point: str):
        """Принимает точку остановки; аварийно завершает отдельный писатель, возвращает None."""
        script = '''
"""Аварийная остановка только тестового процесса публикации.

Функции stop, link, finish имитируют остановки в разных точках.
"""
import os
from pathlib import Path
import sys
from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.memory_storage import file_transaction as t
root, point = Path(sys.argv[1]), sys.argv[2]
store = FileMemoryStore(root / 'memory', ('a.md', 'b.md'))
before = store.snapshot()
after = {'a.md': b'new-a', 'b.md': b'new-b'}
if point == 'add':
    after['0-new.md'] = b'new-document'
    store = FileMemoryStore(root / 'memory', ('0-new.md', 'a.md', 'b.md'))
if point == 'delete':
    del after['a.md']
install = t.install_file
def stop(path, expected, replacement, captured):
    """Принимает замену; завершает процесс после первой установки либо во время отката."""
    if point == 'rollback' and path.name == 'b.md':
        raise OSError('second file failed')
    install(path, expected, replacement, captured)
    if point in ('first', 'add', 'delete') or (point == 'rollback' and 'undo' in captured.parts):
        os._exit(87)
link_original = t.os.link
def link(source, destination):
    """Принимает пути; завершает процесс после захвата оригинала до установки нового файла."""
    if point == 'capture' and Path(destination).name == 'a.md':
        os._exit(87)
    return link_original(source, destination)
recover = t.recover_transaction
def finish(store, **options):
    """Принимает хранилище; завершает процесс после фиксации committed до очистки."""
    manifest = t.journal_path(store.root) / 'transaction.json'
    if point == 'commit' and manifest.exists():
        import json
        if json.loads(manifest.read_text())['phase'] == 'committed':
            os._exit(87)
    return recover(store, **options)
t.install_file, t.os.link, t.recover_transaction = stop, link, finish
store.publish(before, after, root / 'run')
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(self.base), point],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 87, result.stderr)

    def test_crash_after_first_file_restores_before_read(self):
        """Останавливает процесс после первой замены; проверяет восстановление полного before."""
        self.crash("first")
        self.assertEqual((self.root / "a.md").read_bytes(), b"new-a")
        self.assertEqual(self.store.snapshot(), self.before)
        self.assertFalse(transaction.journal_path(self.root).exists())

    def test_crash_after_capture_restores_missing_file(self):
        """Останавливает процесс между захватом и установкой; проверяет возврат оригинала."""
        self.crash("capture")
        self.assertFalse((self.root / "a.md").exists())
        self.assertEqual(self.store.snapshot(), self.before)

    def test_crash_after_new_document_removes_uncommitted_addition(self):
        """Останавливает добавление документа; проверяет удаление незавершённой новой записи."""
        self.crash("add")
        self.assertTrue((self.root / "0-new.md").exists())
        self.assertEqual(self.store.snapshot(), self.before)
        self.assertFalse((self.root / "0-new.md").exists())

    def test_crash_after_deletion_restores_removed_document(self):
        """Останавливает публикацию после удаления; проверяет восстановление удалённых байтов."""
        self.crash("delete")
        self.assertFalse((self.root / "a.md").exists())
        self.assertEqual(self.store.snapshot(), self.before)

    def test_crash_during_rollback_can_recover_again(self):
        """Прерывает восстановление; проверяет повторяемость следующего восстановления."""
        self.crash("rollback")
        self.assertEqual(self.store.snapshot(), self.before)

    def test_committed_publication_survives_crash(self):
        """Прерывает очистку после committed; проверяет сохранение всей новой версии."""
        self.crash("commit")
        self.assertEqual(self.store.snapshot(), self.after)

    def test_validate_only_does_not_recover_or_write(self):
        """Передаёт recover=False после сбоя; проверяет ошибку и отсутствие изменений."""
        self.crash("first")
        original = {str(path): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(RuntimeError, "восстановить"):
            self.store.snapshot(recover=False)
        self.assertEqual({str(path): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}, original)

    def test_rollback_preserves_concurrent_writer(self):
        """Воспроизводит прежнюю потерю правок двух файлов; проверяет сохранение обеих внешних версий."""
        original = transaction.install_file

        def change_after_first(path, expected, replacement, captured):
            """Принимает замену; после первого файла имитирует внешнюю запись двух документов."""
            original(path, expected, replacement, captured)
            if path.name == "a.md" and "captured" in captured.parts:
                (self.root / "a.md").write_bytes(b"external-a")
                (self.root / "b.md").write_bytes(b"external-b")

        with patch.object(transaction, "install_file", side_effect=change_after_first):
            with self.assertRaisesRegex(RuntimeError, "изменился"):
                self.store.publish(self.before, self.after, self.base / "run")
        self.assertEqual(self.store.snapshot(), {"a.md": b"external-a", "b.md": b"external-b"})

    def test_atomic_install_does_not_replace_file_created_during_race(self):
        """Создаёт внешний файл после захвата оригинала; проверяет отсутствие затирания."""
        original = os.link
        created = False

        def create_before_link(source, destination):
            """Принимает пути; однократно создаёт внешнюю версию перед установкой ссылки."""
            nonlocal created
            if Path(destination) == self.root / "a.md" and not created:
                created = True
                Path(destination).write_bytes(b"racing writer")
            return original(source, destination)

        with patch.object(transaction.os, "link", side_effect=create_before_link):
            with self.assertRaises(RuntimeError):
                self.store.publish(self.before, self.after, self.base / "run")
        self.assertEqual((self.root / "a.md").read_bytes(), b"racing writer")
        self.assertEqual((self.root / "b.md").read_bytes(), b"old-b")

    def test_external_change_after_crash_is_preserved(self):
        """После аварии изменяет candidate; проверяет сохранение внешней правки при восстановлении."""
        self.crash("first")
        (self.root / "a.md").write_bytes(b"external-after-crash")
        self.assertEqual(self.store.snapshot(), {"a.md": b"external-after-crash", "b.md": b"old-b"})
        recovery = json.loads((self.base / "run/recovery.json").read_text(encoding="utf-8"))
        self.assertIn("a.md", recovery["external_changes_preserved"])

    def test_reader_cannot_observe_partial_publication(self):
        """Читает снимок во время первой замены; проверяет блокировку промежуточного состояния."""
        original = transaction.install_file

        def inspect_reader(path, expected, replacement, captured):
            """Принимает замену; проверяет отказ конкурирующему читателю до завершения транзакции."""
            original(path, expected, replacement, captured)
            with self.assertRaisesRegex(RuntimeError, "обрабатывается"):
                self.store.snapshot()

        with patch.object(transaction, "install_file", side_effect=inspect_reader):
            self.store.publish(self.before, self.after, self.base / "run")
        self.assertEqual(self.store.snapshot(), self.after)

    def test_read_session_blocks_publication_for_whole_task(self):
        """Удерживает контекст чтения; проверяет запрет публикации до завершения задачи."""
        with self.store.read_session() as snapshot:
            self.assertEqual(snapshot, self.before)
            with self.assertRaisesRegex(RuntimeError, "обрабатывается"):
                self.store.publish(self.before, self.after, self.base / "blocked")
        self.store.publish(self.before, self.after, self.base / "success")
        self.assertEqual(self.store.snapshot(), self.after)

    def test_two_external_versions_are_retained_for_resolution(self):
        """Создаёт разные внешние версии в оригинале и архиве; проверяет остановку без потери обеих."""
        self.crash("first")
        captured = transaction.journal_path(self.root) / "captured/a.md"
        captured.write_bytes(b"external-old-handle")
        (self.root / "a.md").write_bytes(b"external-new-handle")
        with self.assertRaisesRegex(RuntimeError, "двух внешних версий"):
            self.store.snapshot()
        self.assertEqual(captured.read_bytes(), b"external-old-handle")
        self.assertEqual((self.root / "a.md").read_bytes(), b"external-new-handle")

    def test_missing_hardlink_support_keeps_originals(self):
        """Имитирует неподдерживаемые жёсткие ссылки; проверяет отсутствие потери оригинала."""
        with patch.object(transaction.os, "link", side_effect=OSError("Hard links unsupported")):
            with self.assertRaises(OSError):
                self.store.publish(self.before, self.after, self.base / "run")
        self.assertEqual(self.store.snapshot(), self.before)


if __name__ == "__main__":
    unittest.main()
