"""Сборка решающего агента и запись сообщений диалога.

Вход: рабочая область, настройки local_settings/agent_settings.py, сообщения и идентификаторы.
Выход: агент DeepAgents, соединение SQLite и поток сохранённых ответов.
Функции: create_agent — сборка; open_chat_database — открытие и подготовка базы;
save_message — запись сообщения; run_turn — выполнение одной реплики.
"""
from datetime import datetime, timezone
import json
import locale
import os
from pathlib import Path
import sqlite3
import sys
from typing import TYPE_CHECKING, Iterator
from command_agent_memory.model_execution.runtime_environment import describe_execution_environment

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langgraph.graph.state import CompiledStateGraph


def create_agent(workspace: Path, *, model_name: str | None = None,
                 memory_enabled: bool = True,
                 read_only_paths: tuple[str, ...] = ()) -> "CompiledStateGraph":
    """Принимает рабочую область; возвращает агента с личной памятью и skills.

    model_name выбирает модель, memory_enabled включает память. read_only_paths
    перечисляет виртуальные файлы и каталоги, запись в которые запрещена. Используется
    базовый системный промпт DeepAgents с описанием фактической среды. Модель выбирается через model_name,
    OPENROUTER_MODEL либо local_settings/agent_settings.py,
    ключ — из OPENROUTER_API_KEY либо local_settings/agent_settings.py. Новый InMemorySaver изолирует
    контекст экземпляра. Подключает индексы личных и командных знаний и skills.
    Сборка агента сама по себе не отправляет запрос модели.
    """
    from deepagents import create_deep_agent
    from command_agent_memory.model_execution.local_shell import RobustLocalShellBackend
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from command_agent_memory.model_execution.openrouter_api import openrouter_api_key, OPENROUTER_TIMEOUT_SECONDS, OPENROUTER_MAX_RETRIES

    default_model = os.environ.get("OPENROUTER_MODEL", "")
    if not model_name and not default_model:
        from command_agent_memory.application_config import read_local_settings

        default_model = read_local_settings().get("model", "")
    if not model_name and not default_model:
        raise ValueError("Укажи model_name или OPENROUTER_MODEL.")

    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise FileNotFoundError(f"Рабочая область не найдена: {workspace}")
    model = ChatOpenAI(
        model=model_name or default_model, api_key=openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
        timeout=OPENROUTER_TIMEOUT_SECONDS, max_retries=OPENROUTER_MAX_RETRIES,
    )
    backend = RobustLocalShellBackend(
        root_dir=workspace, virtual_mode=True, inherit_env=True,
        read_only_paths=read_only_paths,
        env={
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": locale.getpreferredencoding(False) + ":backslashreplace",
        },
    )
    memory = []
    skills = []
    if memory_enabled:
        for index in ("AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki/INDEX.md",
                      "knowledge/INDEX.md", "team_knowledge/INDEX.md"):
            if (workspace / index).is_file():
                memory.append("/" + index)
        for folder in ("skills", "knowledge/skills", "team_knowledge/skills"):
            if (workspace / folder).is_dir():
                skills.append("/" + folder + "/")
    return create_deep_agent(
        model=model, backend=backend, skills=skills,
        system_prompt=describe_execution_environment(workspace, file_paths="virtual", shell=True),
        memory=memory, checkpointer=InMemorySaver(),
    )


def open_chat_database(path: Path) -> sqlite3.Connection:
    """Принимает путь SQLite; возвращает открытое соединение с таблицей messages.

    Существующие сообщения сохраняются. Каталог должен существовать.
    Вызывающий код отвечает за закрытие соединения.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            user_id TEXT NOT NULL, created_at TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, message_json TEXT NOT NULL)""")
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def save_message(connection: sqlite3.Connection, session_id: str, user_id: str,
                 message: "BaseMessage") -> None:
    """Принимает соединение, ID диалога/пользователя и сообщение; записывает его, возвращает None.

    Полное сообщение, включая вызовы инструментов и usage при наличии,
    сохраняется в message_json. Каждая запись фиксируется сразу.
    """
    from langchain_core.messages import message_to_dict

    content = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)
    connection.execute(
        "INSERT INTO messages (session_id, user_id, created_at, role, content, message_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, str(user_id), datetime.now(timezone.utc).isoformat(),
         "user" if message.type == "human" else message.type, content,
         json.dumps(message_to_dict(message), ensure_ascii=False)),
    )
    connection.commit()


def run_turn(agent: "CompiledStateGraph", text: str, connection: sqlite3.Connection,
             session_id: str, user_id: str, callbacks: list | None = None,
             recursion_limit: int | None = None) -> Iterator["BaseMessage"]:
    """Принимает агента, текст, соединение и ID; выдаёт сохранённые ответы и сообщения инструментов.

    Сначала сохраняет реплику пользователя. Одинаковый session_id продолжает
    контекст агента. При сбое уже полученные сообщения остаются в базе.
    Необязательные callbacks передаются для измерения выполнения.
    recursion_limit задаёт предел шагов графа при проверочном запуске.
    """
    from langchain_core.messages import BaseMessage, HumanMessage

    message = HumanMessage(content=text)
    save_message(connection, session_id, user_id, message)
    config = {"configurable": {"thread_id": session_id}}
    if callbacks:
        config["callbacks"] = callbacks
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    for event in agent.stream(
        {"messages": [message]},
        config=config, stream_mode="updates",
    ):
        if not isinstance(event, dict):
            continue
        for state in event.values():
            if not isinstance(state, dict):
                continue
            for response in state.get("messages", []) or []:
                if isinstance(response, BaseMessage):
                    save_message(connection, session_id, user_id, response)
                    yield response
