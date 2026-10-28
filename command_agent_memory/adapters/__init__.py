"""Адаптеры исполнителей моделей.

Экспортируются ModelAdapter, Runner, load_model_adapter, register_model_adapter
и registered_model_adapters — контракт, фабрика и регистрация провайдеров.
"""
from command_agent_memory.adapters.base import ModelAdapter, Runner
from command_agent_memory.adapters.registry import (
    load_model_adapter, register_model_adapter, registered_model_adapters,
)

__all__ = ["ModelAdapter", "Runner", "load_model_adapter", "register_model_adapter", "registered_model_adapters"]

