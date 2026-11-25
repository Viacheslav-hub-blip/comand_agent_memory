"""Создание и продолжение рабочих сессий replay через штатный агент-хост.

HostReplay: __init__ принимает канал задач, create создаёт сессию.
HostReplaySession: __init__ хранит контекст, turn возвращает ответ на реплику,
close уведомляет управляющего агента о завершении сессии.
"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.model_execution.host_tasks import HostTasks, write_json
from command_agent_memory.worker_agents import WorkerSession, validate_worker_session


BOOTSTRAP = (
    "Решай пользовательскую задачу в переданном рабочем каталоге. Используй только его входы и память, "
    "прочитай местные инструкции агента, если они есть. Храни состояние между репликами. "
    "Читай и изменяй только разрешённые файлы этого каталога. Память и входные файлы доступны для чтения. "
    "Дождись первой пользовательской реплики."
)


class HostReplaySession:
    """Хранит ID независимой сессии, её рабочий каталог и канал продолжения."""

    def __init__(self, tasks: HostTasks, model: str, workspace: Path, session_id: str):
        """Принимает сведения о созданной сессии; сохраняет состояние для следующих ходов."""
        self.tasks = tasks
        self.model = model
        self.workspace = workspace
        self.session_id = session_id

    def turn(self, text: str) -> str:
        """Принимает точную исходную реплику; возвращает ответ той же внешней сессии."""
        prompt = self.tasks.directory / f"turn-{uuid4().hex}.md"
        prompt.write_text(text, encoding="utf-8")
        reply = json.loads(self.tasks.request("replay_turn", self.model, self.workspace, prompt,
                                             (str(self.workspace),), session_id=self.session_id))
        if (not isinstance(reply, dict) or reply.get("session_id") != self.session_id
                or not isinstance(reply.get("answer"), str) or not reply["answer"].strip()):
            raise ValueError("Replay получил пустой ответ или ответ другой сессии.")
        return reply["answer"]

    def close(self) -> None:
        """Без аргументов передаёт хосту ID завершённой сессии; возвращает None."""
        write_json(self.tasks.directory / f"close-{uuid4().hex}.json", {"session_id": self.session_id})


class HostReplay:
    """Подключает две штатные сессии по одному шаблону запуска."""

    def __init__(self, tasks: HostTasks, model: str):
        """Принимает канал задач и выбор модели хоста; сохраняет настройки фабрики."""
        self.tasks = tasks
        self.model = model

    def create(self, workspace: Path, layout: MemoryLayout | None) -> WorkerSession:
        """Принимает независимую копию и границы памяти; возвращает сессию со сверяемым профилем."""
        prompt = self.tasks.directory / f"start-{uuid4().hex}.md"
        prompt.write_text(BOOTSTRAP, encoding="utf-8")
        digest = hashlib.sha256(BOOTSTRAP.encode("utf-8")).hexdigest()
        reply = json.loads(self.tasks.request("replay_start", self.model, workspace, prompt,
                                             instructions_sha256=digest,
                                             memory_paths=list(layout.entries) if layout else []))
        if (not isinstance(reply, dict) or not isinstance(reply.get("session_id"), str)
                or not reply["session_id"].strip()):
            raise ValueError("Хост должен вернуть фактический ID новой независимой сессии replay.")
        agent = HostReplaySession(self.tasks, self.model, workspace, reply["session_id"])
        profile = reply.get("profile")
        session = WorkerSession(agent, profile, agent.close, agent.turn, agent.session_id)
        try:
            validate_worker_session(session)
            if (profile["system_prompt_sha256"] != digest or not profile["tools"]
                    or (self.model != "host-default" and profile["model"] != self.model)):
                raise ValueError("Хост вернул другой шаблон запуска, модель или пустой список инструментов.")
        except BaseException:
            session.close()
            raise
        return session
