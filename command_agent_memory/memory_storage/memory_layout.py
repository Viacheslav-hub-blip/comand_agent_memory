"""Состав и назначение документов памяти.

MemoryLayout принимает личные пути и пути знаний; проверяет границы и пересечения.
Методы: __post_init__, entries, allows, instructions. Функций вне класса нет.
PERSONAL_MEMORY_ENTRIES сохраняет прежний состав памяти для запусков без layout.
"""
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


PERSONAL_MEMORY_ENTRIES = ("AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki", "knowledge")


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

    def allows(self, name: str) -> bool:
        """Принимает имя документа; проверяет относительный путь и возвращает принадлежность области."""
        MemoryLayout((name,))
        return any(Path(name).is_relative_to(Path(entry)) for entry in self.entries)

    def instructions(self) -> str:
        """Без входов возвращает явное описание назначения и границ файлов для LLM."""
        return ("Состав памяти задан пользователем и имеет приоритет над примерами путей в skills. "
                f"Личные правила и контекст: {list(self.personal)}. "
                f"Знания, cases и skills: {list(self.knowledge)}. "
                "Сохраняй существующую структуру. Создавай и изменяй только перечисленные пути "
                "и файлы внутри перечисленных каталогов. Не создавай стандартные каталоги, "
                "если они не перечислены в конфигурации.")
