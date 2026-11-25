"""Проверка опубликованного личного снимка для командной обработки.

Функция read_accepted_knowledge возвращает статус и принятые документы знаний.
"""
import hashlib
import json
from pathlib import Path

from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.memory_storage.memory_snapshots import read_memory
from command_agent_memory.skill_resources import ROOT


def read_accepted_knowledge(run: Path) -> tuple[dict, dict[str, bytes]]:
    """Принимает личный запуск; возвращает его метаданные и принятые файлы knowledge.

    Отклонённый запуск, отсутствие записи о публикации или изменение снимка
    вызывают ValueError. Личные правила, предпочтения и user_llm_wiki не экспортируются.
    """
    run = run.resolve()
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    configured = "memory_layout" in status
    if not configured and (run == ROOT or not run.is_relative_to(ROOT)):
        raise ValueError("Принятый запуск должен находиться внутри проекта.")
    if status.get("stage") != "personal" or status.get("state") not in ("published", "unchanged"):
        raise ValueError(f"Личная память не была принята: {run}")
    if not status.get("user_id") or not isinstance(status.get("snapshot_sha256"), dict):
        raise ValueError(f"Нет владельца или хешей принятой версии: {run}")
    if status.get("replay_status") not in ("not_requested", "skipped", "completed"):
        raise ValueError(f"Нет завершённого основания принятия: {run}")
    if status["replay_status"] == "completed" and status.get("replay_choice") != "candidate":
        raise ValueError(f"Проверка не выбрала предложенную память: {run}")
    target = Path(status.get("target", "")).resolve()
    if ((not configured and (target == ROOT or not target.is_relative_to(ROOT) or not target.is_dir()))
            or (configured and target.exists() and not target.is_dir())):
        raise ValueError(f"Некорректная область владельца принятого снимка: {run}")
    layout = MemoryLayout(tuple(status["memory_layout"]["personal"]),
                          tuple(status["memory_layout"]["knowledge"])) if configured else None
    snapshot = read_memory(run / "after", "personal", layout)
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in snapshot.items()}
    if hashes != status["snapshot_sha256"]:
        raise ValueError(f"Принятый снимок изменён: {run}")
    files = {name.removeprefix("knowledge/"): content for name, content in snapshot.items()
             if name.startswith("knowledge/")}
    if layout is not None:
        files = {name: content for name, content in snapshot.items()
                 if any(Path(name).is_relative_to(Path(entry)) for entry in layout.knowledge)}
    return status, files
