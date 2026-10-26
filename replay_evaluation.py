"""Совместимый импорт подсистемы command_agent_memory.evaluation.replay.

Функций и классов здесь не определяется. Старое имя ссылается на тот же
модуль, сохраняя прежние импорты и границы подмены исполнителей в тестах.
"""
import sys
from command_agent_memory.evaluation import replay as implementation

sys.modules[__name__] = implementation
