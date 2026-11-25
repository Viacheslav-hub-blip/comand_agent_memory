"""Доставка опубликованной локальной командной памяти рабочему агенту.

Функция sync_team_memory принимает источник, рабочую область и журнал;
обновляет team_knowledge/ с проверкой путей, резервированием и журналом версии.
"""
import hashlib
import json
from pathlib import Path

from command_agent_memory.memory_storage.file_storage import FileMemoryStore, checked_path
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import read_memory
from command_agent_memory.memory_storage.team_registry import REGISTRY_PATH, load_registry, active_documents


def sync_team_memory(source: Path, workspace: Path, run: Path) -> None:
    """Принимает опубликованный каталог, личную область и журнал; обновляет team_knowledge/.

    Источник, область получателя и журнал могут находиться вне проекта.
    Возвращает None. Синхронизация предназначена для запуска между задачами агента.
    Личные файлы вне team_knowledge/ не изменяются; прежняя копия резервируется.
    Служебный реестр с pending/conflicts проверяется, но получателю не передаётся.
    """
    source, workspace, run = source.absolute(), workspace.absolute(), run.absolute()
    if not workspace.is_dir() or workspace == Path(workspace.anchor):
        raise ValueError("Нужна существующая рабочая область получателя.")
    target = checked_path(workspace, "team_knowledge")
    checked_path(source, "INDEX.md")
    checked_path(run, "status.json")
    paths = [source.resolve(), target.resolve(), run.resolve()]
    if any(left.is_relative_to(right) or right.is_relative_to(left)
           for index, left in enumerate(paths) for right in paths[index + 1:]):
        raise ValueError("Источник, копия и журнал должны быть раздельными каталогами.")
    with memory_lock(source):
        after = read_memory(source, "knowledge")
        if REGISTRY_PATH in after:
            after = active_documents(load_registry(after))
    if "INDEX.md" not in after:
        raise ValueError("В опубликованной командной памяти нужен INDEX.md.")
    store = FileMemoryStore(workspace, ("team_knowledge",))
    documents = {"team_knowledge/" + name: content for name, content in after.items()}
    run.mkdir(parents=True, exist_ok=False)
    with memory_lock(target):
        before = store.snapshot()
        store.publish(before, documents, run)
        status = {"state": "published" if before != documents else "unchanged",
                  "source": str(source.resolve()), "target": str(target.resolve()),
                  "snapshot_sha256": {name: hashlib.sha256(content).hexdigest()
                                      for name, content in after.items()}}
        (run / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
