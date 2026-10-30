"""Контракт и регистрация хранилищ принятой памяти.

MemoryStore описывает идентичность, защищённые пути, снимок и публикацию.
register_memory_store регистрирует фабрику; load_memory_store создаёт хранилище.
Фабрика получает настройки, каталог конфигурации и MemoryLayout.
"""
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from command_agent_memory.pipeline_config import resolve_path
from command_agent_memory.memory.file_storage import FileMemoryStore, MemoryLayout


class MemoryStore(Protocol):
    """Описывает хранилище документов; модели работают только с копией его снимка."""

    @property
    def identity(self) -> str:
        """Без входов возвращает стабильный идентификатор области памяти без секретов."""
        ...

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        """Без входов возвращает локальные пути хранилища; удалённое возвращает пустой кортеж."""
        ...

    def snapshot(self) -> dict[str, bytes]:
        """Без входов возвращает относительные имена документов и их полное содержимое."""
        ...

    def publish(self, before: dict[str, bytes], after: dict[str, bytes], run: Path) -> None:
        """Принимает версии и журнал; проверяет before и сохраняет after либо вызывает ошибку."""
        ...


StoreFactory = Callable[[dict, Path, MemoryLayout], MemoryStore]
_FACTORIES: dict[str, StoreFactory] = {}


def register_memory_store(name: str, factory: StoreFactory) -> None:
    """Принимает уникальное имя и фабрику; регистрирует хранилище, возвращает None."""
    if not isinstance(name, str) or not name.strip() or name in {"files", "sqlite", *_FACTORIES} or not callable(factory):
        raise ValueError(f"Некорректная регистрация хранилища: {name}")
    _FACTORIES[name] = factory


def load_memory_store(options: dict, base: Path, layout: MemoryLayout) -> MemoryStore:
    """Принимает memory, каталог JSON и состав памяти; возвращает выбранное хранилище.

    Если type не указан, выбирается files с root. Для sqlite нужен path.
    Пользовательский backend самостоятельно проверяет свои настройки.
    """
    kind = options.get("type", "files")
    common = {"type", "personal", "knowledge"}
    if kind == "files":
        if set(options) - common - {"root"}:
            raise ValueError("Неизвестные параметры файловой памяти.")
        return FileMemoryStore(resolve_path(base, options.get("root")), layout.entries)
    if kind == "sqlite":
        from command_agent_memory.memory.sqlite_storage import SQLiteMemoryStore

        if set(options) - common - {"path"}:
            raise ValueError("Неизвестные параметры SQLite-памяти.")
        return SQLiteMemoryStore(resolve_path(base, options.get("path")), layout.entries)
    if isinstance(kind, str) and kind in _FACTORIES:
        return _FACTORIES[kind](options, base, layout)
    raise ValueError(f"Неизвестное хранилище памяти: {kind}")
