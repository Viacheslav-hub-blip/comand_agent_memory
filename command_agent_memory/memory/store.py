"""Настраиваемая файловая память рабочего агента вне каталога проекта.

Классы: MemoryLayout — состав и назначение памяти; FileMemoryStore — снимки и запись.
Функции: checked_path — проверка относительного пути и ссылок.
Методы MemoryLayout: __post_init__, entries, instructions; методы FileMemoryStore:
__init__, allows, snapshot, _write, publish.
"""
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import tempfile


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


@dataclass(frozen=True)
class MemoryLayout:
    """Принимает относительные пути личной памяти и знаний; задаёт две области консолидации."""

    personal: tuple[str, ...]
    knowledge: tuple[str, ...] = ()

    def __post_init__(self):
        """Проверяет непустой состав и непересекающиеся пути; возвращает None."""
        if not self.entries:
            raise ValueError("Перечисли хотя бы один файл или каталог памяти.")
        for index, name in enumerate(self.entries):
            if not isinstance(name, str):
                raise ValueError("Пути памяти должны быть строками.")
            relative = Path(name)
            if (not name or relative.anchor or PureWindowsPath(name).drive or ".." in relative.parts
                    or name == "." or "\\" in name or ":" in name):
                raise ValueError(f"Недопустимый относительный путь памяти: {name}")
            if any(Path(name).is_relative_to(Path(other)) or Path(other).is_relative_to(Path(name))
                   for other in self.entries[:index]):
                raise ValueError(f"Пути памяти пересекаются: {name}")

    @property
    def entries(self) -> tuple[str, ...]:
        """Без входов возвращает все настроенные файлы и каталоги памяти."""
        return self.personal + self.knowledge

    def instructions(self) -> str:
        """Без входов возвращает явное описание назначения и границ файлов для LLM."""
        return ("Состав памяти задан пользователем и имеет приоритет над примерами путей в skills. "
                f"Личные правила и контекст: {list(self.personal)}. "
                f"Знания, cases и skills: {list(self.knowledge)}. "
                "Сохраняй существующую структуру. Создавай и изменяй только перечисленные пути "
                "и файлы внутри перечисленных каталогов. Не создавай стандартные каталоги, "
                "если они не перечислены в конфигурации.")


class FileMemoryStore:
    """Принимает корень и разрешённые пути; читает снимки и применяет изменения с откатом."""

    def __init__(self, root: Path, entries: tuple[str, ...]):
        """Принимает каталог памяти и относительные пути; сохраняет абсолютный корень."""
        self.root = root.absolute()
        self.entries = entries
        for name in entries:
            checked_path(self.root, name)

    def allows(self, name: str) -> bool:
        """Принимает относительное имя; возвращает принадлежность настроенной области памяти."""
        checked_path(self.root, name)
        return any(Path(name).is_relative_to(Path(entry)) for entry in self.entries)

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
        from command_agent_memory.memory.personal import write_snapshot

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
