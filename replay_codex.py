"""Один проверочный запуск задачи через Codex с выбранной моделью.

Вход: копия памяти и входов, запрос, CLI и модель; выход: ответ, трасса и измерения.
Функция run_codex_replay выполняет задачу один раз и сохраняет доступные метрики.
"""
import json
from pathlib import Path
import shutil
from typing import Callable

from codex_client import run_codex


def run_codex_replay(workspace: Path, request: Path, result: Path,
                     executable: str, model: str, runner: Callable | None = None,
                     backend: str = "codex") -> None:
    """Принимает копию окружения, запрос, каталог результата, CLI и модель; возвращает None.

    Агент получает только задачу, входные файлы и новую память. Прежний ответ
    остаётся вне рабочей области. Число внутренних обращений к модели CLI
    не сообщает: model_calls сохраняется как null, а не как число turns.
    При ошибке сохраняются доступная трасса и измерения, исключение передаётся выше.
    """
    runner = runner or run_codex
    result.mkdir(parents=True, exist_ok=False)
    prefix = result / "agent"
    prompt = (
        f"Рабочая область: {workspace.as_posix()}. Реши один запрос пользователя. "
        "Используй только файлы этой рабочей области; сеть и внешние сервисы не используй. "
        "Сначала прочитай существующие AGENTS.md, USER_PREFERENCES.md, user_llm_wiki/INDEX.md "
        "и knowledge/INDEX.md. Найди подходящие skills в skills/ и knowledge/skills/ "
        "и прочитай применимые инструкции и статьи по ссылкам. Память используй только "
        "для чтения. Результаты задачи сохраняй внутри рабочей области. Если в запросе "
        "указаны пути вида /documents/ или /synthetic_team/, разрешай их относительно "
        "физического корня этой рабочей области. Исходные данные не изменяй. "
        "В итоговом ответе сообщи полученный результат и созданные файлы.\n\n"
        "Запрос пользователя:\n" + request.read_text(encoding="utf-8")
    )
    state = "failed"
    try:
        answer = runner(executable, model, prompt, workspace, prefix, writable=True)
        if not answer.strip():
            raise RuntimeError("Исполнитель не выдал итоговый ответ задачи.")
        (result / "answer.md").write_text(answer, encoding="utf-8")
        state = "completed"
    finally:
        event_path = prefix.with_suffix(".jsonl")
        events = []
        if event_path.is_file():
            shutil.copy2(event_path, result / "transcript.jsonl")
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        measurements = prefix.with_suffix(".metrics.json")
        source = json.loads(measurements.read_text(encoding="utf-8")) if measurements.is_file() else {}
        tools = [event["item"] for event in events if event.get("type") == "item.completed"
                 and event.get("item", {}).get("type") in
                 ("command_execution", "mcp_tool_call", "web_search", "file_change")]
        usage = source.get("usage")
        metrics = {
            "state": state, "backend": backend, "model": model,
            "duration_seconds": source.get("duration_seconds"),
            "model_calls": None, "tool_calls": len(tools),
            "tool_calls_scope": "completed CLI command, MCP, search and file-change items",
            "usage": usage, "usage_complete": bool(source.get("completed") and usage is not None),
        }
        (result / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
