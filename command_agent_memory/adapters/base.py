"""Общие контракты адаптеров моделей.

Классы: Runner — сигнатура вызова модели; ModelAdapter — готовый исполнитель
для агентного и одиночного режима. Функции отсутствуют.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Runner(Protocol):
    """Описывает вызов модели и возвращаемый текст ответа.

    Вход: исполняемый файл, модель, prompt, рабочий каталог, префикс журналов
    и разрешение записи. Выход: непустой текст ответа модели.
    """

    def __call__(self, executable: str, model: str, prompt: str, cwd: Path,
                 prefix: Path, writable: bool = False) -> str:
        """Выполняет один модельный запрос и возвращает итоговый текст."""
        ...


@dataclass(frozen=True)
class ModelAdapter:
    """Хранит подготовленный backend модели и его два режима выполнения.

    ``agent_runner`` работает с файловыми инструментами, ``chat_runner`` — без
    них. ``executable`` содержит путь CLI либо пустую строку для API-backend.
    """

    name: str
    executable: str
    agent_runner: Runner
    chat_runner: Runner

