"""Файловые инструменты модельного этапа с явной областью записи и без shell.

ScopedFilesystemBackend принимает корень чтения и абсолютные пути записи.
Методы __init__, _resolve_path, _can_write, write, edit, delete и upload_files проверяют область
перед изменениями. Чтение сохраняет интерфейс FilesystemBackend.
"""
from pathlib import Path
import os

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult

from command_agent_memory.memory_storage.file_storage import checked_path


class ScopedFilesystemBackend(FilesystemBackend):
    """Принимает корень и разрешённые пути; предоставляет чтение и ограниченную запись без execute."""

    def __init__(self, root: Path, write_paths: tuple[str, ...]):
        """Принимает рабочий каталог и абсолютные пути; создаёт backend, возвращает None."""
        super().__init__(root_dir=root, virtual_mode=False)
        self._write_paths = tuple(Path(path).absolute() for path in write_paths)

    def _resolve_path(self, key: str) -> Path:
        """Принимает путь инструмента; возвращает путь ОС, включая /drives/C/... в Windows."""
        if os.name == "nt" and key.startswith("/drives/"):
            parts = key.split("/")
            if len(parts) < 4 or len(parts[2]) != 1 or not parts[2].isascii() or not parts[2].isalpha():
                raise OSError("Нужен путь вида /drives/C/каталог/файл.")
            key = parts[2] + ":/" + "/".join(parts[3:])
        return super()._resolve_path(key)

    def _can_write(self, file_path: str) -> bool:
        """Принимает путь инструмента; возвращает разрешение записи с проверкой ссылок."""
        try:
            path = self._resolve_path(file_path).absolute()
            for root in self._write_paths:
                if path.is_relative_to(root):
                    checked_path(root.parent, path.relative_to(root.parent).as_posix())
                    return True
        except (OSError, ValueError, RuntimeError):
            pass
        return False

    def write(self, file_path: str, content: str) -> WriteResult:
        """Принимает путь и текст; создаёт разрешённый файл либо возвращает ошибку записи."""
        if not self._can_write(file_path):
            return WriteResult(error=f"Запись вне области этапа запрещена: {file_path}")
        return super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Принимает путь и замену; правит разрешённый файл либо возвращает ошибку записи."""
        if not self._can_write(file_path):
            return EditResult(error=f"Запись вне области этапа запрещена: {file_path}")
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        """Принимает путь; удаляет разрешённый файл или каталог либо возвращает ошибку."""
        if not self._can_write(file_path):
            return DeleteResult(error=f"Удаление вне области этапа запрещено: {file_path}")
        return super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Принимает пути и байты; записывает только разрешённые файлы, возвращает результаты."""
        return [super(ScopedFilesystemBackend, self).upload_files([(path, content)])[0]
                if self._can_write(path) else FileUploadResponse(path=path, error="permission_denied")
                for path, content in files]
