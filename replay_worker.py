"""Выполнение одной задачи агентом пользователя в подготовленной копии.

Вход: CLI-пути рабочей области, запроса и результата, user_id и settings.py.
Выход: ответ, SQLite, JSONL-трасса и измерения, включая при сбое.
Класс ReplayMetrics: __init__ — счётчики; on_chat_model_start — запросы модели;
on_tool_start — вызовы инструментов; on_llm_end — учёт сообщённого usage.
Функция main выполняет одну реплику без повторных запусков.
"""
import argparse
import json
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from agent_runtime import create_agent, open_chat_database, run_turn
from memory_pipeline import export_chat_history


class ReplayMetrics(UsageMetadataCallbackHandler):
    """Собирает callbacks всех дочерних вызовов; хранит число обращений и доступный usage."""

    def __init__(self):
        """Без входов создаёт счётчики и блокировку; возвращает None."""
        super().__init__()
        self.model_calls = 0
        self.tool_calls = 0
        self.usage_calls = 0
        self.counts_lock = Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        """Принимает описание модели и сообщения; увеличивает число запросов, возвращает None."""
        with self.counts_lock:
            self.model_calls += 1

    def on_tool_start(self, serialized, input_str, **kwargs):
        """Принимает описание инструмента и вход; увеличивает счётчик, возвращает None."""
        with self.counts_lock:
            self.tool_calls += 1

    def on_llm_end(self, response, **kwargs):
        """Принимает результат LLM; суммирует usage и отмечает его наличие, возвращает None."""
        super().on_llm_end(response, **kwargs)
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        if (isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage)
                and generation.message.usage_metadata and generation.message.response_metadata.get("model_name")):
            with self.counts_lock:
                self.usage_calls += 1


def main() -> None:
    """Читает CLI и settings.py; выполняет один запрос и сохраняет результат, возвращает None.

    При сбое записывает доступные измерения и трассу, затем передаёт исключение.
    Время включает сборку агента. Ответы прежнего выполнения агенту не передаются.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    args.result.mkdir(parents=True, exist_ok=False)
    database = args.result / "chat_logs.db"
    connection = open_chat_database(database)
    session_id = str(uuid4())
    callback = ReplayMetrics()
    started = perf_counter()
    state = "failed"
    answer = ""
    try:
        agent = create_agent(workspace)
        task = args.request.read_text(encoding="utf-8")
        request = (
            f"Физический корень текущей рабочей области: {workspace.as_posix()}. "
            "В execute используй пути внутри этого корня, даже если в памяти указан прежний корень. "
            "Память AGENTS.md, USER_PREFERENCES.md, user_llm_wiki/, knowledge/ и skills/ "
            "используй только для чтения. Результаты задачи записывай в рабочую область.\n\n"
            f"Запрос пользователя:\n{task}"
        )
        for message in run_turn(agent, request, connection, session_id, args.user_id, callbacks=[callback]):
            message.pretty_print()
            if isinstance(message, AIMessage) and not message.tool_calls:
                answer = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)
        if not answer.strip():
            raise RuntimeError("Агент не выдал итоговый ответ.")
        (args.result / "answer.md").write_text(answer, encoding="utf-8")
        state = "completed"
    finally:
        connection.close()
        metrics = {
            "state": state, "duration_seconds": perf_counter() - started,
            "model_calls": callback.model_calls, "tool_calls": callback.tool_calls,
            "usage": callback.usage_metadata or None,
            "usage_complete": callback.model_calls > 0 and callback.usage_calls == callback.model_calls,
        }
        (args.result / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        export_chat_history(database, args.user_id, session_id, args.result / "transcript.jsonl")


if __name__ == "__main__":
    main()
