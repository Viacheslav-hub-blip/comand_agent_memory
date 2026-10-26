"""CLI полного цикла схемы для одного или нескольких пользователей.

Вход: аргументы либо --users с JSON-заданиями. Выход: отчёты и принятая память.
Функций и классов нет; main определена в command_agent_memory.pipeline.
"""
from command_agent_memory.pipeline import main

if __name__ == "__main__":
    main()
