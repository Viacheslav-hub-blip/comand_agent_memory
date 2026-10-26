"""Прямой запуск агента через OpenRouter без Codex CLI.

Вход: модель OpenRouter, ключ из окружения либо settings.py, задание и рабочая область.
Выход: последний ответ агента; prompt, JSONL-трасса, stderr, ответ и metrics.json.
Функции: openrouter_api_key — чтение ключа; validate_openrouter — проверка окружения;
_message_text — извлечение текста; run_openrouter_chat — один вызов без инструментов;
run_openrouter — запуск агента.
"""
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4


OPENROUTER_TIMEOUT_SECONDS = 180
OPENROUTER_MAX_RETRIES = 0


def openrouter_api_key() -> str:
    """Без входов возвращает ключ из окружения либо settings.py; при отсутствии вызывает ValueError."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        import settings

        key = settings.openrouter_api_key
    if not key:
        raise ValueError("Задай OPENROUTER_API_KEY или openrouter_api_key в settings.py.")
    return key


def validate_openrouter() -> None:
    """Проверяет ключ и библиотеки OpenRouter-исполнителя; возвращает None.

    Отсутствующий ключ или библиотека вызывают исключение до сетевого запроса.
    Значение ключа не читается и не записывается в журналы.
    """
    openrouter_api_key()
    try:
        import deepagents  # noqa: F401
        import langchain_openai  # noqa: F401
    except ImportError as error:
        raise RuntimeError("Для OpenRouter нужны пакеты deepagents и langchain-openai.") from error


def _message_text(content: Any) -> str:
    """Принимает содержимое сообщения LangChain; возвращает объединённый текст."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content) if content is not None else ""


def run_openrouter_chat(_runtime: str, model: str, prompt: str, cwd: Path,
                        prefix: Path, writable: bool = False) -> str:
    """Принимает задание и модель; выполняет один запрос без инструментов и возвращает текст.

    cwd и writable сохранены для общего интерфейса исполнителей. Функция пишет
    prompt, ответ, JSONL-сообщение и metrics.json. Файлы рабочей области модели
    недоступны, поэтому этот режим подходит для анализа уже переданного текста.
    """
    validate_openrouter()
    from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, message_to_dict
    from langchain_openai import ChatOpenAI

    del cwd, writable
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
    callback = UsageMetadataCallbackHandler()
    chat_model = ChatOpenAI(
        model=model,
        api_key=openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
        timeout=OPENROUTER_TIMEOUT_SECONDS,
        max_retries=OPENROUTER_MAX_RETRIES,
    )
    started = perf_counter()
    completed = False
    try:
        response = chat_model.invoke([HumanMessage(content=prompt)], config={"callbacks": [callback]})
        answer = _message_text(response.content)
        if not answer.strip():
            raise RuntimeError("OpenRouter не выдал текст отчёта.")
        prefix.with_suffix(".last.txt").write_text(answer, encoding="utf-8")
        prefix.with_suffix(".jsonl").write_text(
            json.dumps(message_to_dict(response), ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        prefix.with_suffix(".stderr.log").write_text("", encoding="utf-8")
        completed = True
        return answer
    except BaseException as error:
        prefix.with_suffix(".stderr.log").write_text(
            f"{type(error).__name__}: {error}\n", encoding="utf-8",
        )
        raise
    finally:
        prefix.with_suffix(".metrics.json").write_text(json.dumps({
            "backend": "openrouter", "mode": "single-call", "model": model,
            "duration_seconds": perf_counter() - started, "completed": completed,
            "usage": getattr(callback, "usage_metadata", None) or None,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_openrouter(_runtime: str, model: str, prompt: str, cwd: Path,
                   prefix: Path, writable: bool = False) -> str:
    """Принимает совместимые параметры исполнителя; возвращает последний ответ агента.

    Первый параметр сохранён для единого интерфейса с Codex и не используется.
    Модель вызывается напрямую через OpenRouter. Агент получает файловые и shell
    инструменты в cwd. При writable=False промпт запрещает изменения файлов.
    Доступные сообщения и usage сохраняются даже при сбое.
    """
    validate_openrouter()
    from deepagents import create_deep_agent
    from langchain_core.callbacks.usage import UsageMetadataCallbackHandler
    from langchain_core.messages import AIMessage, HumanMessage, message_to_dict
    from langchain_openai import ChatOpenAI
    from robust_local_shell import RobustLocalShellBackend

    cwd = cwd.resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
    trace_path = prefix.with_suffix(".jsonl")
    error_path = prefix.with_suffix(".stderr.log")
    output_path = prefix.with_suffix(".last.txt")
    instruction = prompt
    if not writable:
        instruction += "\n\nФайлы доступны только для чтения. Не создавай, не изменяй и не удаляй их."
    callback = UsageMetadataCallbackHandler()
    chat_model = ChatOpenAI(
        model=model,
        api_key=openrouter_api_key(),
        base_url="https://openrouter.ai/api/v1",
        timeout=OPENROUTER_TIMEOUT_SECONDS,
        max_retries=OPENROUTER_MAX_RETRIES,
    )
    backend = RobustLocalShellBackend(root_dir=cwd, virtual_mode=False, inherit_env=True)
    agent = create_deep_agent(
        model=chat_model,
        backend=backend,
        system_prompt=(
            "Выполни одну переданную задачу. Не создавай подзадачи и не делегируй работу. "
            "Сразу прочитай указанные файлы, внеси требуемые изменения и проверь их. "
            "Не повторяй одинаковые вызовы инструментов. После проверки верни итоговый ответ."
        ),
    )
    messages = []
    seen_messages = set()
    started = perf_counter()
    try:
        with trace_path.open("w", encoding="utf-8") as trace:
            for event in agent.stream(
                {"messages": [HumanMessage(content=instruction)]},
                config={"configurable": {"thread_id": str(uuid4())}, "callbacks": [callback],
                        "recursion_limit": 1000},
                stream_mode="updates",
            ):
                if not isinstance(event, dict):
                    continue
                for state in event.values():
                    if not isinstance(state, dict):
                        continue
                    for message in state.get("messages", []) or []:
                        record = message_to_dict(message)
                        marker = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                        if marker in seen_messages:
                            continue
                        seen_messages.add(marker)
                        messages.append(message)
                        trace.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                        trace.flush()
        answer = next(
            (_message_text(message.content) for message in reversed(messages)
             if isinstance(message, AIMessage) and _message_text(message.content).strip()),
            "",
        )
        if not answer:
            raise RuntimeError("OpenRouter-агент не выдал итоговый ответ.")
        output_path.write_text(answer, encoding="utf-8")
        error_path.write_text("", encoding="utf-8")
        completed = True
        return answer
    except BaseException as error:
        error_path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        completed = False
        raise
    finally:
        usage = getattr(callback, "usage_metadata", None)
        metrics = {
            "backend": "openrouter", "model": model,
            "duration_seconds": perf_counter() - started,
            "completed": completed if "completed" in locals() else False,
            "usage": usage or None,
        }
        prefix.with_suffix(".metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
