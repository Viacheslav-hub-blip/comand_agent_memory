"""Контракты и регистрация исполнителей моделей.

Классы: Runner — сигнатура вызова; ModelAdapter — два режима исполнителя.
Функции: register_model_adapter — регистрация фабрики; registered_model_adapters —
список имён; load_model_adapter — ленивая загрузка выбранного исполнителя.
"""
from collections.abc import Callable
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


ModelFactory = Callable[[Path | None], ModelAdapter]
_FACTORIES: dict[str, ModelFactory] = {}


def register_model_adapter(name: str, factory: ModelFactory) -> None:
    """Принимает уникальное имя и фабрику (путь CLI → ModelAdapter); регистрирует провайдера."""
    if not isinstance(name, str) or not name.strip() or name in registered_model_adapters() or not callable(factory):
        raise ValueError(f"Имя провайдера занято либо фабрика некорректна: {name}")
    _FACTORIES[name] = factory


def registered_model_adapters() -> tuple[str, ...]:
    """Без входов возвращает имена встроенных и зарегистрированных провайдеров."""
    return ("codex", "openrouter", *_FACTORIES)


def load_model_adapter(name: str, executable_path: Path | None = None) -> ModelAdapter:
    """Принимает имя backend и необязательный путь CLI; возвращает готовый адаптер.

    Для ``codex`` функция находит исполняемый файл. Для ``openrouter`` проверяет
    ключ и зависимости. Неизвестное имя вызывает ``ValueError``.
    """
    if name in _FACTORIES:
        return _FACTORIES[name](executable_path)
    if name == "codex":
        from command_agent_memory.model_providers.codex_cli import find_codex, run_codex

        return ModelAdapter(
            name=name,
            executable=find_codex(executable_path),
            agent_runner=run_codex,
            chat_runner=run_codex,
        )
    if name == "openrouter":
        from command_agent_memory.model_providers.openrouter_api import run_openrouter, run_openrouter_chat, validate_openrouter

        validate_openrouter()
        return ModelAdapter(
            name=name,
            executable="",
            agent_runner=run_openrouter,
            chat_runner=run_openrouter_chat,
        )
    raise ValueError(f"Неизвестный backend модели: {name}")

