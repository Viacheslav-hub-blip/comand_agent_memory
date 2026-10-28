"""Фабрика зарегистрированных исполнителей моделей.

Функции: load_model_adapter — проверка backend и создание адаптера Codex либо
OpenRouter. Импорты выполняются лениво, поэтому необязательные SDK не требуются
для просмотра структуры проекта и запуска другого backend.
register_model_adapter регистрирует фабрику; registered_model_adapters возвращает имена.
"""
from pathlib import Path

from command_agent_memory.adapters.base import ModelAdapter

_FACTORIES = {}


def register_model_adapter(name: str, factory) -> None:
    """Принимает уникальное имя и фабрику (путь CLI → ModelAdapter); регистрирует провайдера."""
    if not name or name in registered_model_adapters() or not callable(factory):
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
        from codex_client import find_codex, run_codex

        return ModelAdapter(
            name=name,
            executable=find_codex(executable_path),
            agent_runner=run_codex,
            chat_runner=run_codex,
        )
    if name == "openrouter":
        from openrouter_client import run_openrouter, run_openrouter_chat, validate_openrouter

        validate_openrouter()
        return ModelAdapter(
            name=name,
            executable="",
            agent_runner=run_openrouter,
            chat_runner=run_openrouter_chat,
        )
    raise ValueError(f"Неизвестный backend модели: {name}")

