"""Запуск анализа диалогов напрямую через OpenRouter.

Вход: аргументы analyze_chats_codex.py без --backend.
Выход: два отчёта на чат и журналы OpenRouter.
Функция main добавляет backend и модель по умолчанию, затем запускает анализатор.
"""
from pathlib import Path
import runpy
import sys


def main() -> None:
    """Принимает аргументы командной строки; запускает анализ через OpenRouter и возвращает None."""
    arguments = sys.argv[1:]
    if "--backend" not in arguments:
        arguments = ["--backend", "openrouter", *arguments]
    if "--model" not in arguments:
        arguments = ["--model", "openai/gpt-5.6-luna", *arguments]
    sys.argv = ["analyze_chats_codex.py", *arguments]
    runpy.run_path(str(Path(__file__).with_name("analyze_chats_codex.py")), run_name="__main__")


if __name__ == "__main__":
    main()
