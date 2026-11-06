"""Запись сообщений штатного рабочего агента и совместимый импорт его сборки.

create_agent импортируется из working_agent и используется также в обычном запуске.
Функции: open_chat_database подготавливает SQLite; save_message сохраняет сообщение;
run_turn выполняет ход в существующей сессии и выдаёт сообщения с инструментальными результатами.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Iterator

from command_agent_memory.working_agent import create_agent

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langgraph.graph.state import CompiledStateGraph


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
