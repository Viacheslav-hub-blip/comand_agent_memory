"""Измерения для replay-агентов, использующих инструменты LangChain.

ReplayMetrics: __init__ создаёт счётчики; on_chat_model_start, on_tool_start
и on_llm_end записывают обращения и измерения моделей. Функций вне класса нет.
"""
from threading import Lock

from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration


class ReplayMetrics(UsageMetadataCallbackHandler):
    """Собирает callbacks дочерних вызовов, хранит число обращений и доступный usage."""

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
        """Принимает результат модели; суммирует usage и отмечает его наличие, возвращает None."""
        super().on_llm_end(response, **kwargs)
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        if (isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage)
                and generation.message.usage_metadata and generation.message.response_metadata.get("model_name")):
            with self.counts_lock:
                self.usage_calls += 1
