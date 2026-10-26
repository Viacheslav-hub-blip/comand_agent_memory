"""Фабрика зарегистрированных исполнителей моделей.

Функции: load_model_adapter — проверка backend и создание адаптера Codex либо
OpenRouter. Импорты выполняются лениво, поэтому необязательные SDK не требуются
для просмотра структуры проекта и запуска другого backend.
"""
from pathlib import Path

from command_agent_memory.adapters.base import ModelAdapter


def load_model_adapter(name: str, executable_path: Path | None = None) -> ModelAdapter:
    """Принимает имя backend и необязательный путь CLI; возвращает готовый адаптер.

    Для ``codex`` функция находит исполняемый файл. Для ``openrouter`` проверяет
    ключ и зависимости. Неизвестное имя вызывает ``ValueError``.
    """
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

