"""Сценарии и исполнители синтетических диалогов.

Экспортируется load_dialogues — независимая от конкретной модели загрузка
сценариев, пригодная для повторного использования разными раннерами.
"""
from command_agent_memory.synthetic.scenarios import load_dialogues

__all__ = ["load_dialogues"]

