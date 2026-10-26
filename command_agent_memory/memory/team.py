"""Объединение принятых личных знаний и доставка командного снимка.

Вход: завершённые личные запуски, каталог командной памяти и исполнитель LLM.
Выход: принятая общая версия, журналы и копии team_knowledge/ у пользователей.
Функции: read_accepted_knowledge — проверка публикации и чтение знаний;
consolidate_team_memory — одно объединение в копии без отдельного review;
sync_team_memory — перенос опубликованных знаний в выбранную рабочую область.
Реестр подтверждений, pending и conflicts намеренно не реализованы.
"""
import hashlib
import json
from pathlib import Path
from typing import Callable

from memory_pipeline import ROOT
from command_agent_memory.memory.personal import (
    KNOWLEDGE_SKILL, memory_lock, parse_review_decision, publish_memory,
    read_memory, read_rules, write_snapshot,
)
from run_reports import write_stage_report


def read_accepted_knowledge(run: Path) -> tuple[dict, dict[str, bytes]]:
    """Принимает личный запуск; возвращает его метаданные и принятые файлы knowledge.

    Отклонённый запуск, отсутствие записи о публикации или изменение снимка
    вызывают ValueError. Личные правила, предпочтения и user_llm_wiki не экспортируются.
    """
    run = run.resolve()
    if run == ROOT or not run.is_relative_to(ROOT):
        raise ValueError("Принятый запуск должен находиться внутри проекта.")
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    if status.get("stage") != "personal" or status.get("state") not in ("published", "unchanged"):
        raise ValueError(f"Личная память не была принята: {run}")
    if not status.get("user_id") or not isinstance(status.get("snapshot_sha256"), dict):
        raise ValueError(f"Нет владельца или хешей принятой версии: {run}")
    if status.get("replay_status") not in ("not_requested", "skipped", "completed"):
        raise ValueError(f"Нет завершённого основания принятия: {run}")
    if status["replay_status"] == "completed" and status.get("replay_choice") != "candidate":
        raise ValueError(f"Проверка не выбрала предложенную память: {run}")
    target = Path(status.get("target", "")).resolve()
    if target == ROOT or not target.is_relative_to(ROOT) or not target.is_dir():
        raise ValueError(f"Некорректная область владельца принятого снимка: {run}")
    snapshot = read_memory(run / "after", "personal")
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in snapshot.items()}
    if hashes != status["snapshot_sha256"]:
        raise ValueError(f"Принятый снимок изменён: {run}")
    files = {name.removeprefix("knowledge/"): content for name, content in snapshot.items()
             if name.startswith("knowledge/")}
    return status, files


def consolidate_team_memory(target: Path, accepted_runs: list[Path], run: Path,
                            executable: str, model: str, runner: Callable) -> dict:
    """Принимает общую память, личные запуски и LLM; возвращает состояние публикации.

    Входы проверяются до вызова модели. Один консолидатор объединяет знания и
    сообщает accepted либо оставшиеся проблемы. Отдельного агента-review нет.
    При отказе, сбое или изменении источников общая память не публикуется.
    """
    target, run = target.resolve(), run.resolve()
    if (target == ROOT or run == ROOT or not target.is_relative_to(ROOT)
            or not run.is_relative_to(ROOT) or target.is_relative_to(run) or run.is_relative_to(target)):
        raise ValueError("Командная память и запуск должны быть раздельными каталогами проекта.")
    accepted_runs = list(dict.fromkeys(path.resolve() for path in accepted_runs))
    if not accepted_runs:
        raise ValueError("Передайте хотя бы один принятый личный запуск.")
    sources = [(path, *read_accepted_knowledge(path)) for path in accepted_runs]
    if any(target.is_relative_to(path) or path.is_relative_to(target) for path in accepted_runs):
        raise ValueError("Командная память не должна пересекаться с личными запусками.")
    if any(target.is_relative_to(Path(metadata["target"])) or Path(metadata["target"]).is_relative_to(target)
           for _, metadata, _ in sources):
        raise ValueError("Командная память не должна пересекаться с личными областями.")
    run.mkdir(parents=True, exist_ok=False)
    status = {"stage": "team", "target": str(target), "state": "running",
              "registry": "not_implemented", "accepted_runs": [str(path) for path in accepted_runs]}
    try:
        with memory_lock(target):
            before = read_memory(target, "knowledge")
            write_snapshot(run / "before", before)
            draft = run / "workspace/memory"
            write_snapshot(draft, before)
            if not any(files for _, _, files in sources):
                write_snapshot(run / "after", before)
                status.update(state="unchanged", reason="В личных снимках нет новых командных знаний.")
                status["snapshot_sha256"] = {
                    name: hashlib.sha256(content).hexdigest() for name, content in before.items()
                }
                write_stage_report(run, "team-consolidation", "Объединение принятых знаний", "not_requested",
                                   status["reason"])
                return status
            materials = []
            for source, metadata, files in sources:
                materials.append(json.dumps({
                    "user_id": metadata["user_id"], "accepted_run": str(source),
                    "replay_status": metadata["replay_status"],
                    "analysis_sessions": metadata.get("analysis_sessions", []),
                }, ensure_ascii=False))
                for name, content in sorted(files.items()):
                    materials.append(f"\n## {name}\nИсточник: {source / 'after/knowledge' / name}\n")
                    try:
                        materials.append(content.decode("utf-8-sig"))
                    except UnicodeDecodeError:
                        materials.append("Бинарное приложение; используй исходный файл по указанному пути.")
            (run / "materials.md").write_text("\n\n".join(materials), encoding="utf-8")
            (run / "rules.md").write_text(read_rules(KNOWLEDGE_SKILL), encoding="utf-8")
            prompt = (
                f"Правила: {(run / 'rules.md').as_posix()}. "
                f"Принятые знания пользователей: {(run / 'materials.md').as_posix()}. "
                f"Текущая копия командной памяти: {draft.as_posix()}. "
                "Объедини wiki, Cases, Skills и INDEX. Изменяй только копию; источники не меняй. "
                "Проверь смысл, условия, повторы и противоречия в рамках этого объединения. "
                "Не публикуй личные предпочтения и полномочия. Реестра подтверждений пока нет: "
                "не выдумывай подтверждения, голосование или состояния pending/conflicts. "
                "При неразрешённом противоречии верни needs_resolution. "
                "Верни JSON: {\"decision\":\"accepted|needs_resolution\",\"summary\":\"пояснение\","
                "\"findings\":[]}. В оставшихся findings укажи file, problem, change. "
                "При accepted findings должен быть пустым."
            )
            write_stage_report(run, "team-consolidation", "Объединение принятых знаний", "running",
                               "Выполняется один сеанс консолидации без отдельного review.")
            reply = runner(executable, model, prompt, run / "workspace", run / "consolidation", writable=True)
            decision = parse_review_decision(reply)
            (run / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
            after = read_memory(draft, "knowledge")
            write_snapshot(run / "after", after)
            if decision["decision"] != "accepted" or decision["findings"]:
                status["state"] = "rejected"
                raise RuntimeError(f"Командное объединение не принято: {decision['summary']}")
            if after and "INDEX.md" not in after:
                raise ValueError("В командной версии нужен INDEX.md для подключения агенту.")
            for source, metadata, files in sources:
                if read_accepted_knowledge(source) != (metadata, files):
                    raise RuntimeError("Источник принятых знаний изменился во время объединения.")
            if after != before:
                publish_memory(target, before, after, "knowledge", run)
            elif read_memory(target, "knowledge") != before:
                raise RuntimeError("Командная память изменилась во время объединения.")
            status["state"] = "published" if after != before else "unchanged"
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


def sync_team_memory(source: Path, workspace: Path, run: Path) -> None:
    """Принимает опубликованный каталог, личную область и журнал; обновляет team_knowledge/.

    Правила и личные знания пользователя не изменяются. Обновление командного
    снимка использует резервирование и проверку конкурирующей записи.
    """
    workspace, run = workspace.resolve(), run.resolve()
    if workspace == ROOT or not workspace.is_relative_to(ROOT) or not workspace.is_dir():
        raise ValueError("Рабочая область должна существовать внутри проекта.")
    target = workspace / "team_knowledge"
    source = source.resolve()
    if (source == ROOT or not source.is_relative_to(ROOT) or run == ROOT
            or not run.is_relative_to(ROOT) or run.is_relative_to(target) or target.is_relative_to(run)):
        raise ValueError("Источник, копия и журнал должны быть раздельными каталогами проекта.")
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Источник команды и подключаемая копия должны быть раздельными.")
    after = read_memory(source, "knowledge")
    if after and "INDEX.md" not in after:
        raise ValueError("В опубликованной командной памяти нужен INDEX.md.")
    run.mkdir(parents=True, exist_ok=False)
    with memory_lock(target):
        before = read_memory(target, "knowledge")
        if before != after:
            publish_memory(target, before, after, "knowledge", run)
