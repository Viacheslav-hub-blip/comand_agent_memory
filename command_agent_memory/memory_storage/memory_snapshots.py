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
    try:
        return FileMemoryStore(directory, PERSONAL_MEMORY_ENTRIES if stage == "personal" else (),
                               whole_directory=stage == "knowledge").snapshot()
    except ValueError as error:
        raise RuntimeError(str(error)) from error


def write_snapshot(directory: Path, files: dict[str, bytes]) -> None:
    """Создаёт снимок; вход — новый каталог и файлы, выход — запись на диск, None."""
    directory.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()):
            raise RuntimeError(f"Путь выходит за снимок: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
