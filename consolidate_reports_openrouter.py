"""Запуск консолидации, ревью и проверки напрямую через OpenRouter.

Вход: аргументы consolidate_reports_codex.py без --backend.
Выход: предложение памяти, ревью, проверочный прогон и опубликованный результат.
Функция main добавляет OpenRouter для всех LLM-этапов и запускает консолидатор.
"""
from pathlib import Path
import runpy
import sys


def main() -> None:
    """Принимает аргументы командной строки; запускает полный цикл OpenRouter и возвращает None."""
    arguments = sys.argv[1:]
    if "--backend" not in arguments:
        arguments = ["--backend", "openrouter", *arguments]
    if "--model" not in arguments:
        arguments = ["--model", "openai/gpt-5.6-luna", *arguments]
    if "--evaluate-by-replay" in arguments and "--replay-backend" not in arguments:
        arguments = ["--replay-backend", "openrouter", *arguments]
    sys.argv = ["consolidate_reports_codex.py", *arguments]
    runpy.run_path(str(Path(__file__).with_name("consolidate_reports_codex.py")), run_name="__main__")


if __name__ == "__main__":
    main()
