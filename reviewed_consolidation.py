"""Совместимый импорт подсистемы command_agent_memory.memory.personal.

Функций и классов здесь не определяется. Старое имя ссылается на тот же
модуль, сохраняя прежние импорты и границы подмены исполнителей в тестах.
"""
import sys
from command_agent_memory.memory import personal as implementation

sys.modules[__name__] = implementation
