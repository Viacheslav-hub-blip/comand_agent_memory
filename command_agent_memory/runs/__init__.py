"""Общие инструменты каталогов запусков.

Экспортируются create_run_directory и new_run_id для единообразного хранения
анализа, консолидации, replay и будущих типов запусков.
"""
from command_agent_memory.runs.layout import create_run_directory, new_run_id

__all__ = ["create_run_directory", "new_run_id"]

