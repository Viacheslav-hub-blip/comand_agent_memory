"""Чтение и публикация файловых документов памяти.

Класс FileMemoryStore хранит параметры и публикует принятую разницу с откатом.
Методы: __init__, identity, protected_paths, allows, snapshot, _write, publish.
Функции: checked_path проверяет путь; publish_memory сохраняет прежний порядок
публикации для запусков без явно настроенного layout.
"""
import os
from pathlib import Path, PureWindowsPath
import tempfile

from command_agent_memory.memory_storage.memory_layout import MemoryLayout, PERSONAL_MEMORY_ENTRIES
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
    """Принимает корень и разрешённые пути; читает снимки и применяет изменения с откатом."""

    def __init__(self, root: Path, entries: tuple[str, ...]):
        """Принимает каталог памяти и относительные пути; сохраняет абсолютный корень."""
        MemoryLayout(entries)
        self.root = root.absolute()
        self.entries = entries
        for name in entries:
            checked_path(self.root, name)

    @property
    def identity(self) -> str:
        """Без входов возвращает нормализованный идентификатор файловой области памяти."""
        return "files:" + os.path.normcase(str(self.root))

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        """Без входов возвращает выбранные локальные пути для проверки пересечений."""
        return tuple(checked_path(self.root, name) for name in self.entries)

    def allows(self, name: str) -> bool:
        """Принимает относительное имя; возвращает принадлежность настроенной области памяти."""
        checked_path(self.root, name)
        return MemoryLayout(self.entries).allows(name)

    def snapshot(self) -> dict[str, bytes]:
        """Без входов возвращает относительные пути и полное содержимое выбранных файлов."""
        result = {}
        pending = [checked_path(self.root, name) for name in self.entries]
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
                raise ValueError(f"Неподдерживаемый файл памяти: {path}")
        return result

    def _write(self, name: str, content: bytes | None) -> None:
        """Принимает имя и байты либо None; заменяет один файл или удаляет его."""
        path = checked_path(self.root, name)
        if content is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            checked_path(self.root, name)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish(self, before: dict[str, bytes], after: dict[str, bytes], run: Path) -> None:
        """Принимает снимки и журнал; проверяет текущую версию, применяет разницу с откатом.

        Резервная копия сохраняется до первой записи. Проверка версии повторяется
        перед каждым файлом. Несколько файлов не публикуются атомарно для читателей.
        """
        from command_agent_memory.memory_storage.memory_snapshots import write_snapshot

        for name in before.keys() | after.keys():
            if not self.allows(name):
                raise ValueError(f"Изменение вне настроенной памяти: {name}")
        if self.snapshot() != before:
            raise RuntimeError("Память рабочего агента изменилась; перенос отменён.")
        write_snapshot(run / "external-backup", before)
        changed = [name for name in sorted(before.keys() | after.keys())
                   if before.get(name) != after.get(name)]
        applied = []
        try:
            for name in changed:
                path = checked_path(self.root, name)
                current = path.read_bytes() if path.is_file() else None
                if current != before.get(name):
                    raise RuntimeError(f"Файл изменился перед записью: {path}")
                self._write(name, after.get(name))
                applied.append(name)
        except BaseException:
            for name in reversed(applied):
                self._write(name, before.get(name))
            raise


def publish_memory(target: Path, before: dict[str, bytes], after: dict[str, bytes],
                   stage: str, run: Path, layout: MemoryLayout | None = None) -> None:
    """Переносит принятую память; вход — цель, две версии, этап и журнал, выход — None.

    Оригиналы сохраняет в backup. При исключении восстанавливает перенесённые
    элементы. Это не атомарная публикация нескольких файлов для читателей.
    layout включает FileMemoryStore и разрешает явно настроенный внешний корень.
    """
    from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot

    if layout is not None:
        FileMemoryStore(target, layout.entries).publish(before, after, run)
        return
    write_snapshot(run / "publish", after)
    if read_memory(target, stage) != before:
        raise RuntimeError("Действующая память изменилась во время обработки; перенос отменён.")
    names = [""] if stage == "knowledge" else PERSONAL_MEMORY_ENTRIES
    moved = []
    try:
        for index, name in enumerate(names):
            destination = target / name if name else target
            if destination.resolve() == ROOT or not destination.resolve().is_relative_to(ROOT):
                raise RuntimeError(f"Недопустимая цель переноса: {destination}")
            source = run / "publish" / name if name else run / "publish"
            backup = run / "backup" / str(index)
            backup.parent.mkdir(parents=True, exist_ok=True)
            existed = destination.exists()
            if existed:
                os.replace(destination, backup)
            moved.append((destination, backup, existed))
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
    except BaseException:
        for index, (destination, backup, existed) in enumerate(reversed(moved)):
            if destination.exists():
                discarded = run / "failed-publish" / str(index)
                discarded.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, discarded)
            if existed:
                os.replace(backup, destination)
        raise
