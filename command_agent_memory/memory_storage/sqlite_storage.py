"""Хранилище документов памяти в SQLite с транзакционной публикацией.

Класс SQLiteMemoryStore: __init__ сохраняет параметры; identity и protected_paths
описывают область; _read читает документы; snapshot возвращает снимок без записи;
publish проверяет версию и применяет изменения в одной транзакции.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from command_agent_memory.memory_storage.file_storage import checked_path
from command_agent_memory.memory_storage.memory_layout import MemoryLayout


class SQLiteMemoryStore:
    """Принимает файл SQLite и разрешённые имена документов; хранит содержимое как BLOB."""

    def __init__(self, path: Path, entries: tuple[str, ...]):
        """Принимает путь и состав; проверяет имена, не создавая файл или таблицу."""
        self.layout = MemoryLayout(entries)
        self.path = path.absolute()
        checked_path(self.path.parent, self.path.name)

    @property
    def identity(self) -> str:
        """Без входов возвращает нормализованный идентификатор файла БД без секретов."""
        return "sqlite:" + os.path.normcase(str(self.path))

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        """Без входов возвращает файл БД и служебные файлы SQLite для проверки пересечений."""
        return tuple(Path(str(self.path) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))

    def _read(self, connection: sqlite3.Connection) -> dict[str, bytes]:
        """Принимает соединение; возвращает выбранные документы либо пустой снимок без таблицы."""
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_documents'"
        ).fetchone()
        if not exists:
            return {}
        return {name: bytes(content) for name, content in connection.execute(
            "SELECT path, content FROM memory_documents ORDER BY path"
        ) if self.layout.allows(name)}

    def snapshot(self) -> dict[str, bytes]:
        """Без входов возвращает снимок через read-only соединение; отсутствующая БД не создаётся."""
        checked_path(self.path.parent, self.path.name)
        if not self.path.exists():
            return {}
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            return self._read(connection)

    def publish(self, before: dict[str, bytes], after: dict[str, bytes], run: Path) -> None:
        """Принимает снимки и журнал; проверяет версию под блокировкой и атомарно публикует after.

        Несовпадение before вызывает RuntimeError. Любая ошибка откатывает
        транзакцию; резервная копия before сохраняется в external-backup.
        """
        from command_agent_memory.memory_storage.memory_snapshots import write_snapshot

        for name in before.keys() | after.keys():
            if not self.layout.allows(name):
                raise ValueError(f"Изменение вне настроенной памяти: {name}")
        checked_path(self.path.parent, self.path.name)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._read(connection) != before:
                    raise RuntimeError("Память в SQLite изменилась; перенос отменён.")
                write_snapshot(run / "external-backup", before)
                connection.execute("CREATE TABLE IF NOT EXISTS memory_documents (path TEXT PRIMARY KEY, content BLOB NOT NULL)")
                for name in sorted(before.keys() | after.keys()):
                    if name in after:
                        connection.execute(
                            "INSERT INTO memory_documents(path, content) VALUES (?, ?) "
                            "ON CONFLICT(path) DO UPDATE SET content=excluded.content", (name, after[name]))
                    else:
                        connection.execute("DELETE FROM memory_documents WHERE path=?", (name,))
