"""Чтение и публикация файловых документов памяти с восстановлением.

Класс FileMemoryStore: __init__ задаёт область; identity и protected_paths описывают
её; allows проверяет документ; _snapshot читает под блокировкой; read_session
фиксирует память на время задачи; snapshot восстанавливает
журнал и читает; publish публикует транзакцию. Функции: checked_path проверяет путь;
publish_memory сохраняет интерфейс публикации для прежних запусков.
"""
import os
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath

from command_agent_memory.memory_storage.memory_layout import MemoryLayout, PERSONAL_MEMORY_ENTRIES
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.file_transaction import journal_path, publish_transaction, recover_transaction
from command_agent_memory.skill_resources import ROOT


def checked_path(root: Path, name: str) -> Path:
    """Принимает корень и относительное имя; возвращает путь без выхода из корня и ссылок."""
    relative = Path(name)
    if (not name or relative.anchor or PureWindowsPath(name).drive
            or ".." in relative.parts or name == "." or "\\" in name or ":" in name):
        raise ValueError(f"Нужен относительный путь памяти с разделителем /: {name}")
    path = root / relative
    for candidate in (root, *root.parents, *[root / Path(*relative.parts[:i])
                                           for i in range(1, len(relative.parts) + 1)]):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError(f"Ссылка в пути памяти не допускается: {candidate}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Путь выходит из области памяти: {name}")
    return path


class FileMemoryStore:
    """Принимает корень и пути; читает согласованные снимки и публикует журналируемые изменения."""

    def __init__(self, root: Path, entries: tuple[str, ...], *, whole_directory: bool = False):
        """Принимает корень, пути и флаг всего каталога; проверяет область, возвращает None."""
        if not whole_directory:
            MemoryLayout(entries)
        self.root = root.absolute()
        self.entries = entries
        self.whole_directory = whole_directory
        checked_path(self.root.parent, self.root.name)
        self.root = self.root.resolve()
        for name in entries:
            checked_path(self.root, name)

    @property
    def identity(self) -> str:
        """Без входов возвращает нормализованный идентификатор файловой области памяти."""
        return 'files:' + os.path.normcase(str(self.root))

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        """Без входов возвращает область памяти и соседний служебный журнал публикации."""
        selected = (self.root,) if self.whole_directory else tuple(
            checked_path(self.root, name) for name in self.entries)
        return (*selected, journal_path(self.root))

    def allows(self, name: str) -> bool:
        """Принимает относительное имя; возвращает принадлежность настроенной области памяти."""
        checked_path(self.root, name)
        return self.whole_directory or MemoryLayout(self.entries).allows(name)

    def _snapshot(self) -> dict[str, bytes]:
        """Без входов возвращает файлы под уже удерживаемой блокировкой публикации."""
        result = {}
        pending = (list(self.root.iterdir()) if self.whole_directory and self.root.is_dir() else
                   [] if self.whole_directory else [checked_path(self.root, name) for name in self.entries])
        while pending:
            path = pending.pop()
            name = path.relative_to(self.root).as_posix()
            checked_path(self.root, name)
            if not path.exists():
                continue
            if path.is_dir():
                pending.extend(sorted(path.iterdir()))
            elif path.is_file():
                result[name] = path.read_bytes()
            else:
                raise ValueError(f'Неподдерживаемый файл памяти: {path}')
        return result

    @contextmanager
    def read_session(self, *, recover: bool = True) -> Iterator[dict[str, bytes]]:
        """Принимает разрешение восстановления; выдаёт снимок, удерживая блокировку до выхода.

        Обёртка рабочего агента может удерживать этот контекст на протяжении задачи:
        новые публикации получат отказ, а незавершённый перенос восстановится до старта.
        """
        with memory_lock('publication:' + self.identity):
            recover_transaction(self, allow_write=recover)
            yield self._snapshot()

    def snapshot(self, *, recover: bool = True) -> dict[str, bytes]:
        """Принимает разрешение восстановления; возвращает согласованный снимок либо ошибку.

        recover=False используется validate-only: незавершённая публикация вызывает
        ошибку без записи. Обычное чтение сначала восстанавливает прерванный перенос.
        """
        with self.read_session(recover=recover) as snapshot:
            return snapshot

    def publish(self, before: dict[str, bytes], after: dict[str, bytes], run: Path) -> None:
        """Принимает снимки и журнал; публикует разницу с восстановлением после остановки.

        Внешние изменения сохраняются. Читатели snapshot не видят промежуточную
        версию; нативного агента следует останавливать на время обновления файлов.
        """
        for name in before.keys() | after.keys():
            if not self.allows(name):
                raise ValueError(f'Изменение вне настроенной памяти: {name}')
        with memory_lock('publication:' + self.identity):
            publish_transaction(self, before, after, run)


def publish_memory(target: Path, before: dict[str, bytes], after: dict[str, bytes],
                   stage: str, run: Path, layout: MemoryLayout | None = None) -> None:
    """Принимает цель, снимки, этап и run; публикует через общий транзакционный путь."""
    if layout is not None:
        FileMemoryStore(target, layout.entries).publish(before, after, run)
        return
    if target.resolve() == ROOT or not target.resolve().is_relative_to(ROOT):
        raise RuntimeError(f'Недопустимая цель переноса: {target}')
    FileMemoryStore(target, PERSONAL_MEMORY_ENTRIES if stage == 'personal' else (),
                    whole_directory=stage == 'knowledge').publish(before, after, run)
