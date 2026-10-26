"""Совместимый CLI полного цикла OpenRouter для существующего источника Codex.

Вход: аргументы пользователя и проектов; выход: полный цикл целевой схемы.
Функций и классов нет. Основная команда — run_memory_pipeline.py;
здесь сохранены прежние значения backend=openrouter и source=codex.
"""
import sys
from command_agent_memory.pipeline import main

if __name__ == "__main__":
    main(["--backend", "openrouter", "--source", "codex", *sys.argv[1:]])
