"""Чтение и публикация решений автономного командного скилла.

Функции: packet готовит данные консолидатора, proposed_snapshot применяет решение
к исходному реестру, team_operation выполняет одну операцию локального или Git-хранилища.
Вложенная apply_git сверяет решение с актуальным основанием вклада.
"""
from pathlib import Path

from command_agent_memory.memory_storage.accepted_knowledge import read_accepted_knowledge
from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.team_registry import (
    apply_updates, load_registry, registry_context, registry_snapshot, source_evidence,
)
from command_agent_memory.run_reports import create_run_directory
from command_agent_memory.team_memory_delivery.contributions import digest, json_bytes
from command_agent_memory.team_memory_delivery.git_team import (
    CONTRIBUTIONS, canonical_snapshot, contribution_metadata, open_repository, process_contribution,
    read_contribution, run_git_team,
)
from command_agent_memory.team_memory_delivery.local_copy import sync_team_memory
from memory_io import read_json


def packet(before: dict, sources: list) -> dict:
    """Принимает командный снимок и проверенные вклады; возвращает материалы и отпечаток задания."""
    registry = load_registry(before)
    evidence = source_evidence(sources)
    documents = {}
    for source, metadata, files in sources:
        owner = metadata.get("contribution_id", str(source))
        documents[owner] = {name: content.decode("utf-8-sig", errors="replace") for name, content in files.items()}
    return {"input_sha256": digest(json_bytes({"registry": registry, "sources": evidence})),
            "registry": registry_context(registry), "documents": documents,
            "sources": registry_context({"entries": {}, "sources": evidence})["sources"]}


def proposed_snapshot(before: dict, sources: list, decision: dict, revision: str) -> dict | None:
    """Принимает решение и текущие источники; возвращает проверенный снимок или None при отказе."""
    if decision["input_sha256"] != packet(before, sources)["input_sha256"]:
        raise ValueError("Основание командного решения изменилось. Получите новый team-input.")
    value = decision["result"]
    if (set(value) != {"decision", "findings", "summary", "updates"} or not isinstance(value["summary"], str)
            or not value["summary"].strip() or not isinstance(value["findings"], list)
            or not isinstance(value["updates"], list)):
        raise ValueError("Неполное решение командного скилла.")
    if value["decision"] in {"changes_requested", "needs_resolution"}:
        return None
    if value["decision"] != "accepted" or value["findings"]:
        raise ValueError("Недопустимое решение командного скилла.")
    registry = apply_updates(load_registry(before), value["updates"], source_evidence(sources), revision)
    return registry_snapshot(registry)


def team_operation(args, git, local: Path | None, recipients: list) -> dict:
    """Принимает операцию, настроенную команду и получателей; возвращает материалы, квитанцию или доставку."""
    if git is None and local is None:
        raise ValueError("Укажите team в конфигурации.")
    result = {}
    accepted = [path.resolve(strict=True) for path in args.accepted]
    if git is not None:
        if args.action in {"team-submit", "team-sync", "team-status"}:
            mode = {"team-submit": "contribute", "team-sync": "sync", "team-status": "status"}[args.action]
            run_git_team(git, mode, accepted, recipients, result)
            return result
        with memory_lock(git.cache_dir):
            repo = open_repository(git)
            before, receipts = canonical_snapshot(repo.tree(repo.fetch(git.main_ref)))
            refs = repo.refs(CONTRIBUTIONS + "*")
            if not args.contribution:
                if args.action != "team-input":
                    raise ValueError("Выберите --contribution для публикации.")
                return {"pending": sorted(ref.removeprefix(CONTRIBUTIONS) for ref in refs
                                          if ref.removeprefix(CONTRIBUTIONS) not in receipts)}
            identifier = args.contribution
            ref = CONTRIBUTIONS + identifier
            if ref not in refs or repo.fetch(ref) != refs[ref]:
                raise ValueError("Ветка вклада отсутствует или изменилась.")
            manifest, files = read_contribution(repo, git, identifier, refs[ref])
            source = (git.cache_dir, contribution_metadata(git, identifier, manifest), files)
            if args.action == "team-input":
                return packet(before, [source])
            decision = read_json(args.decision)

            def apply_git(current, contribution, run):
                """Принимает актуальные материалы Git; возвращает снимок, основанный на решении скилла."""
                snapshot = proposed_snapshot(current, [contribution], decision, identifier)
                (run / "decision.json").write_bytes(json_bytes(decision["result"]))
                return snapshot

            return process_contribution(repo, git, identifier, manifest, files, apply_git)
    if args.action in {"team-submit", "team-status"}:
        raise ValueError("Очередь вкладов требует team.type=git.")
    journals = local.parent / (local.name + ".skill-runs")
    if args.action == "team-sync":
        for workspace in recipients:
            sync_team_memory(local, workspace, create_run_directory(journals) / "delivery")
        return {"state": "synced", "recipients": [str(path) for path in recipients]}
    if not accepted:
        raise ValueError("Укажите --accepted с путями принятых личных запусков.")
    sources = [(path, *read_accepted_knowledge(path)) for path in accepted]
    store = FileMemoryStore(local, (), whole_directory=True)
    with memory_lock(local):
        before = store.snapshot()
        if args.action == "team-input":
            return packet(before, sources)
        run = create_run_directory(journals)
        after = proposed_snapshot(before, sources, read_json(args.decision), run.name)
        if after is None:
            return {"state": "rejected"}
        store.publish(before, after, run)
        return {"state": "published" if before != after else "unchanged", "run": str(run)}
