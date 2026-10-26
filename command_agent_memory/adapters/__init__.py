"""Адаптеры исполнителей моделей.

Экспортируются ModelAdapter, Runner и load_model_adapter — общий контракт и
фабрика Codex/OpenRouter, предназначенная для расширения новыми backend.
"""
from command_agent_memory.adapters.base import ModelAdapter, Runner
from command_agent_memory.adapters.registry import load_model_adapter

__all__ = ["ModelAdapter", "Runner", "load_model_adapter"]

