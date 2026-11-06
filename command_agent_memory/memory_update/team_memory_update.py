"""Проверка принятых знаний сотрудников и обновление командной памяти.

Функции: parse_team_decision извлекает решение из ответа модели;
read_accepted_knowledge проверяет статус и хеши, возвращает знания;
consolidate_team_memory сопоставляет принятые знания с реестром и публикует
действующие документы вместе с версиями и основаниями. Отдельного review нет.
"""
import hashlib
import json
from pathlib import Path
from typing import Callable

from command_agent_memory.skill_resources import ROOT, get_skill_path, read_stage_instructions
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.file_storage import FileMemoryStore, publish_memory
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.memory_update.memory_review import is_review_decision
from command_agent_memory.memory_storage.team_registry import (
    load_registry, source_evidence, registry_context, apply_updates, registry_snapshot,
)
from command_agent_memory.model_execution.execution_policy import execution_policy
from command_agent_memory.run_reports import write_stage_report


def parse_team_decision(reply: str) -> dict:
    """Принимает ответ с возможным Markdown; возвращает последнее решение строгой схемы.

    Полностью разобранные объекты пропускаются целиком, чтобы JSON внутри текста
    документов не подменил внешнее решение. При отсутствии решения вызывает ValueError.
    """
    decoder = json.JSONDecoder()
    position, decisions = 0, []
    while position < len(reply):
        start = reply.find("{", position)
        if start < 0:
            break
        try:
            value, position = decoder.raw_decode(reply, start)
        except ValueError:
            position = start + 1
            continue
        if (isinstance(value, dict)
                and set(value) == {"decision", "summary", "findings", "updates"}
                and isinstance(value["decision"], str) and isinstance(value["updates"], list)
                and is_review_decision({key: item for key, item in value.items() if key != "updates"})):
            decisions.append(value)
    if not decisions:
        raise ValueError("Ответ командного консолидатора не содержит решения строгой схемы.")
    return decisions[-1]


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


def consolidate_team_memory(target: Path, accepted_runs: list[Path], run: Path,
                            executable: str, model: str, runner: Callable, *, configured: bool = False,
                            configuration_sha256: str | None = None, runtime=None) -> dict:
    """Принимает общую память, личные запуски и LLM; возвращает состояние публикации.

    Один консолидатор возвращает решения по знаниям без записи файлов. Приложение
    проверяет решения, сохраняет историю и публикует только accepted-документы.
    Pending/conflicts хранятся в служебном реестре. Отказ или сбой сохраняет снимок.
    configured разрешает явно настроенные внешние каталоги; configuration_sha256
    сохраняет версию моделей и пожеланий для повторного запуска. runtime при
    наличии проверяет неизменность инструкций и настроек перед публикацией.
    """
    target, run = target.resolve(), run.resolve()
    if ((not configured and (target == ROOT or run == ROOT or not target.is_relative_to(ROOT)
                            or not run.is_relative_to(ROOT)))
            or target.is_relative_to(run) or run.is_relative_to(target)):
        raise ValueError("Командная память и запуск должны быть раздельными каталогами проекта.")
    accepted_runs = list(dict.fromkeys(path.resolve() for path in accepted_runs))
    if not accepted_runs:
        raise ValueError("Передайте хотя бы один принятый личный запуск.")
    sources = [(path, *read_accepted_knowledge(path)) for path in accepted_runs]
    if any(target.is_relative_to(path) or path.is_relative_to(target) for path in accepted_runs):
        raise ValueError("Командная память не должна пересекаться с личными запусками.")
    owner_paths = []
    for _, metadata, _ in sources:
        owner = Path(metadata["target"])
        if configured and "memory_layout" in metadata:
            owner_paths.extend(owner / name for group in metadata["memory_layout"].values() for name in group)
        else:
            owner_paths.append(owner)
    if any(target.is_relative_to(owner) or owner.is_relative_to(target) for owner in owner_paths):
        raise ValueError("Командная память не должна пересекаться с личными областями.")
    run.mkdir(parents=True, exist_ok=False)
    status = {"stage": "team", "target": str(target), "state": "running",
              "registry": ".registry/state.json", "accepted_runs": [str(path) for path in accepted_runs]}
    if runtime is not None and configuration_sha256 is None:
        configuration_sha256 = runtime.fingerprint()
    if configuration_sha256 is not None:
        status["configuration_sha256"] = configuration_sha256
    try:
        with memory_lock(target):
            before = read_memory(target, "knowledge")
            write_snapshot(run / "before", before)
            registry = load_registry(before)
            evidence = source_evidence(sources)
            (run / "workspace").mkdir()
            (run / "registry-context.json").write_text(json.dumps(
                registry_context(registry), ensure_ascii=False, indent=2), encoding="utf-8")
            (run / "sources.json").write_text(json.dumps(
                registry_context({"sources": evidence, "entries": {}})["sources"],
                ensure_ascii=False, indent=2), encoding="utf-8")
            materials = []
            for source, metadata, files in sources:
                materials.append(json.dumps({
                    "user_id": metadata["user_id"], "accepted_run": str(source),
                    "replay_status": metadata["replay_status"],
                    "analysis_sessions": metadata.get("analysis_sessions", []),
                }, ensure_ascii=False))
                for name, content in sorted(files.items()):
                    source_root = source / "after" if "memory_layout" in metadata else source / "after/knowledge"
                    materials.append(f"\n## {name}\nИсточник: {(source_root / name).as_posix()}\n")
                    try:
                        materials.append(content.decode("utf-8-sig"))
                    except UnicodeDecodeError:
                        materials.append("Бинарное приложение; используй исходный файл по указанному пути.")
            (run / "materials.md").write_text("\n\n".join(materials), encoding="utf-8")
            (run / "rules.md").write_text(read_stage_instructions(
                get_skill_path("team-memory-consolidation")), encoding="utf-8")
            prompt = (
                f"Правила: {(run / 'rules.md').as_posix()}. "
                f"Принятые знания пользователей: {(run / 'materials.md').as_posix()}. "
                f"Текущий реестр и история решений: {(run / 'registry-context.json').as_posix()}. "
                f"Проверенные новые источники с ID: {(run / 'sources.json').as_posix()}. "
                "Прочитай материалы и сопоставь их с реестром. Верни JSON по skill; файлы не изменяй. "
                "findings содержит только оставшиеся нерешённые проблемы обработки. "
                "При decision=accepted findings обязательно пуст: []. Выполненные изменения "
                "и разрешённые вопросы описывай в summary и updates, а не в findings. "
                "Наличие записей pending/conflicts само по себе не является ошибкой обработки."
            )
            write_stage_report(run, "team-consolidation", "Объединение принятых знаний", "running",
                               "Выполняется один сеанс консолидации без отдельного review.")
            if evidence:
                with execution_policy(write_paths=()):
                    reply = runner(executable, model, prompt, run / "workspace", run / "consolidation")
                decision = parse_team_decision(reply)
            else:
                decision = {"decision": "accepted", "summary": "Новых командных знаний нет.",
                            "findings": [], "updates": []}
            if (not isinstance(decision, dict)
                    or set(decision) != {"decision", "summary", "findings", "updates"}
                    or not is_review_decision({key: value for key, value in decision.items() if key != "updates"})):
                raise ValueError("Неверный протокол решения командного консолидатора.")
            (run / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
            if decision["decision"] != "accepted" or decision["findings"]:
                status["state"] = "rejected"
                raise RuntimeError(f"Командное объединение не принято: {decision['summary']}")
            registry = apply_updates(registry, decision["updates"], evidence, str(run))
            after = registry_snapshot(registry)
            write_snapshot(run / "after", after)
            for source, metadata, files in sources:
                if read_accepted_knowledge(source) != (metadata, files):
                    raise RuntimeError("Источник принятых знаний изменился во время объединения.")
            if runtime is not None and runtime.fingerprint() != configuration_sha256:
                raise RuntimeError("Настройки командного этапа изменились; перенос отменён.")
            if read_memory(target, "knowledge") != before:
                raise RuntimeError("Командная память изменилась во время объединения.")
            if after != before:
                if configured:
                    FileMemoryStore(target, (), whole_directory=True).publish(before, after, run)
                else:
                    publish_memory(target, before, after, "knowledge", run)
            status["state"] = "published" if after != before else "unchanged"
            status["knowledge_states"] = {
                name: sum(entry["versions"][-1]["status"] == name for entry in registry["entries"].values())
                for name in ("accepted", "pending", "conflicts")
            }
            status["snapshot_sha256"] = {
                name: hashlib.sha256(content).hexdigest() for name, content in after.items()
            }
            write_stage_report(run, "team-consolidation", "Объединение принятых знаний", "completed",
                               decision["summary"], before, after)
    except BaseException as error:
        status["error"] = str(error)
        if status["state"] == "running":
            status["state"] = "failed"
        raise
    finally:
        (run / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        write_stage_report(run, "publication", "Командная публикация", status["state"],
                           "Память принята." if status["state"] in ("published", "unchanged")
                           else "Память не принята. " + status.get("error", ""))
    return status
