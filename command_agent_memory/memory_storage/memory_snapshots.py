"""Чтение и материализация рабочих снимков памяти.

Функции: read_memory принимает область, этап и layout, возвращает документы;
write_snapshot принимает новый каталог и документы, сохраняет их на диск.
"""
from pathlib import Path

from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.memory_storage.memory_layout import MemoryLayout, PERSONAL_MEMORY_ENTRIES


def read_memory(directory: Path, stage: str, layout: MemoryLayout | None = None) -> dict[str, bytes]:
    """Читает память; вход — каталог и этап, выход — относительные пути и байты.

    Для personal читает AGENTS.md, USER_PREFERENCES.md, user_llm_wiki и knowledge.
    Для knowledge читает весь каталог. Ссылки и reparse points не допускаются.
    Если задан layout, читает только перечисленные в нём пути внешней памяти.
    """
    if layout is not None:
        return FileMemoryStore(directory, layout.entries).snapshot()
    if directory.exists() or directory.is_symlink():
        attributes = getattr(directory.lstat(), "st_file_attributes", 0)
        if directory.is_symlink() or attributes & 0x400:
            raise RuntimeError(f"Ссылка в области памяти не допускается: {directory}")
    pending = [directory] if stage == "knowledge" else [
        directory / name for name in PERSONAL_MEMORY_ENTRIES
    ]
    files = {}
    while pending:
        path = pending.pop()
        if not path.exists() and not path.is_symlink():
            continue
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & 0x400:
            raise RuntimeError(f"Ссылка в области памяти не допускается: {path}")
        if path.is_dir():
            pending.extend(sorted(path.iterdir()))
        else:
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return files


def write_snapshot(directory: Path, files: dict[str, bytes]) -> None:
    """Создаёт снимок; вход — новый каталог и файлы, выход — запись на диск, None."""
    directory.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()):
            raise RuntimeError(f"Путь выходит за снимок: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
