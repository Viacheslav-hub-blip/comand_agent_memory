"""Штатная тестовая фабрика рабочего агента без моделей и сети.

Функции: create_session создаёт независимую сессию; profile возвращает описание.
FixtureAgent: __init__ задаёт состояние; stream сохраняет диалог; close отмечает завершение.
"""
import hashlib
from pathlib import Path
import platform

from command_agent_memory.worker_agents import WorkerSession


SESSIONS = []


def profile(model: str = "test-model") -> dict:
    """Принимает модель; возвращает воспроизводимый профиль тестового исполнителя."""
    return {"model": model, "model_parameters": {}, "system_prompt_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "tools": [{"name": "fixture_tool", "schema": {"type": "object"}, "implementation": "fixture-v1"}],
            "environment": {"os": platform.system(), "python": platform.python_version(), "memory_enabled": True},
            "implementation": "fixture-v1"}


class FixtureAgent:
    """Принимает копию; хранит отдельный контекст и поддерживает контракт stream."""

    def __init__(self, workspace: Path):
        """Принимает каталог; создаёт пустую историю и флаг закрытия, возвращает None."""
        self.workspace, self.requests, self.closed = workspace, [], False

    def stream(self, inputs, config, stream_mode="updates"):
        """Принимает очередную реплику; выдаёт ответ, сохраняя все запросы этой сессии."""
        from langchain_core.messages import AIMessage

        self.requests.append(inputs["messages"][0].content)
        yield {"agent": {"messages": [AIMessage(content="Задача решена")]}}

    def close(self):
        """Без входов отмечает освобождение ресурсов; возвращает None."""
        self.closed = True


def create_session(workspace: Path, options: dict, memory_layout=None) -> WorkerSession:
    """Принимает копию и параметры; возвращает новый агент с собственным состоянием."""
    agent = FixtureAgent(workspace)
    session = WorkerSession(agent, profile(options.get("model", "test-model")), agent.close)
    SESSIONS.append(session)
    return session
