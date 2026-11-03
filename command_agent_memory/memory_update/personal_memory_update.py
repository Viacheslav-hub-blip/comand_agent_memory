"""Полный цикл обновления памяти одного человека: подготовка, ревью, replay, публикация.

Функция consolidate_with_review принимает выбранную память, материалы, модели
и журнал; публикует только принятый результат, сохраняя статусы и снимки.
"""
import hashlib
import json
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from command_agent_memory.model_execution.codex_cli import run_codex
from command_agent_memory.skill_resources import ROOT
from command_agent_memory.run_reports import write_stage_report
from command_agent_memory.memory_storage.file_storage import publish_memory
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import read_memory
from command_agent_memory.memory_update.memory_proposal import prepare_memory
from command_agent_memory.memory_update.memory_review import review_memory

if TYPE_CHECKING:
    from command_agent_memory.memory_replay.replay_scenario import ReplayScenario
    from command_agent_memory.memory_storage.storage_registry import MemoryStore


def consolidate_with_review(target: Path, stage: str, materials: str, instructions_path: Path,
                            executable: str, model: str, run: Path,
                            review_model: str | None = None,
                            knowledge_materials: str = "",
                            replay_scenario: "ReplayScenario | None" = None,
                            user_id: str = "replay",
                            replay_requested: bool = False, replay_context: Path | None = None,
                            runner: Callable | None = None,
                            layout: MemoryLayout | None = None, runtime=None,
                            store: "MemoryStore | None" = None) -> None:
    """Принимает память, материалы, skills и модели; переносит принятую копию, возвращает None.

    Личный этап готовит правила, контекст и knowledge в одной копии. Блокировка
    удерживается от чтения до переноса; отказ и сбой сохраняют исходную память.
    replay_requested или replay_scenario включает проверку наличия данных.
    При нехватке данных перенос разрешает ревью; иначе выполняется диалог и
    требуется candidate. user_id задаёт владельца принятого снимка. Отчёты
    и хеши версии сохраняются в run/report/ и status.json.
    layout разрешает настроенные пути внешнего агента; runtime маршрутизирует
    модельные этапы и сохраняет снимок настроек и пожеланий.
    store задаёт хранилище принятой памяти; требуется layout. Публикация
    выполняется через его контракт после прежних проверок ревью и replay.
    """
    custom_runner = runner
    runner = runner or run_codex
    if store is not None and layout is None:
        raise ValueError("Для хранилища памяти требуется MemoryLayout.")
    target = target.resolve() if layout is None else target.absolute()
    run = run.resolve()
    replay_enabled = replay_requested or replay_scenario is not None
    if replay_enabled and stage != "personal":
        raise ValueError("Проверочный прогон применяется только к личному этапу.")
    default_invalid = layout is None and (target == ROOT or run == ROOT
                      or not target.is_relative_to(ROOT) or not run.is_relative_to(ROOT)
                      or target.is_relative_to(run) or run.is_relative_to(target))
    configured_invalid = layout is not None and any(
        (target / name).is_relative_to(run) or run.is_relative_to(target / name) for name in layout.entries)
    if stage not in ("personal", "knowledge") or default_invalid or configured_invalid:
        raise RuntimeError("Память и материалы запуска должны быть раздельными каталогами внутри проекта.")
    run.mkdir(parents=True, exist_ok=False)
    if runtime is not None:
        runtime.save(run / "configuration")
    status = {"stage": stage, "target": str(target), "user_id": str(user_id), "state": "running",
              "replay_status": "pending" if replay_enabled else "not_requested"}
    if layout is not None:
        status["memory_layout"] = {"personal": list(layout.personal), "knowledge": list(layout.knowledge)}
    if runtime is not None:
        status["configuration_sha256"] = runtime.fingerprint()
    if store is not None:
        status["memory_store_identity"] = store.identity
    try:
        with memory_lock(store.identity if store is not None else target):
            if replay_context is not None:
                from command_agent_memory.history_analysis.analysis_reports import report_fingerprint

                status["report_fingerprint"] = report_fingerprint(replay_context)
            links = {"Полные извлечённые сведения": run / "materials.md"}
            if replay_context is not None:
                links["Входные отчёты и журналы"] = replay_context
            write_stage_report(run, "analysis", "Извлечённые сведения", "completed",
                               "Переданы результаты анализа контекста и решений. Их полные тексты "
                               "сохраняются в materials.md без разбора разделов.", links=links)
            prepare_arguments = (
                target, stage, materials, instructions_path, executable, model, run, knowledge_materials,
            )
            if layout is not None or runtime is not None:
                proposal = prepare_memory(*prepare_arguments, runner=runner, layout=layout, runtime=runtime,
                                          **({"store": store} if store is not None else {}))
            else:
                proposal = (prepare_memory(*prepare_arguments, runner=runner)
                            if custom_runner is not None else prepare_memory(*prepare_arguments))
            before_review = proposal.after
            write_stage_report(run, "review", "Проверка и исправления", "running", "Ревьюер проверяет копию памяти.")
            proposal, decision = (
                review_memory(proposal, executable, review_model or model,
                              runner=runtime.runner("review") if runtime is not None else runner)
                if custom_runner is not None or runtime is not None
                else review_memory(proposal, executable, review_model or model)
            )
            write_stage_report(run, "review", "Проверка и исправления", decision["decision"],
                               decision.get("summary", "Решение сохранено в журнале ревью."),
                               before_review, proposal.after,
                               {"Предложение до ревью": run / "before-review", "Результат ревью": run / "after",
                                "Решение": run / "reviews/review-001.json"})
            status["reviews"] = decision["reviews"]
            if decision["decision"] != "accepted":
                status.update(state="rejected", review=decision["review"])
                raise RuntimeError(f"Память не обновлена: {decision['summary']} См. {decision['review']}")
            if replay_enabled:
                from command_agent_memory.memory_replay.replay_scenario import check_replay_data
                from command_agent_memory.memory_replay.replay_evaluation import evaluate_by_replay

                assessment = check_replay_data(replay_scenario)
                assessment_directory = run / "replay-assessment"
                assessment_directory.mkdir()
                decision_path = assessment_directory / "decision.json"
                decision_path.write_text(json.dumps(assessment, ensure_ascii=False, indent=2), encoding="utf-8")
                status["replay_assessment"] = str(decision_path)
                write_stage_report(run, "replay-assessment", "Наличие данных для диалога", "completed",
                                   assessment["summary"], links={"Проверка данных": decision_path})
                if assessment["decision"] == "skip":
                    status.update(replay_status="skipped", replay_reason=assessment["summary"])
                    write_stage_report(run, "replay", "Проверка решением задачи", "skipped",
                                       assessment["summary"] + "\n\nПрогон не выполнен. Принятие памяти основано на ревью.")
                else:
                    if replay_scenario is None:
                        raise RuntimeError("Для диалогового прогона не предоставлен сценарий.")
                    status.update(replay_status="running", replay=str(run / "replay"))
                    write_stage_report(run, "replay", "Проверка решением задачи", "running", "Выполняется один прогон.")
                    try:
                        evaluation_arguments = (
                            proposal, replay_scenario, executable, review_model or model,
                            user_id,
                        )
                        evaluation = (evaluate_by_replay(
                            *evaluation_arguments, runner=runner,
                            **({"runtime": runtime} if runtime is not None else {}),
                        ) if custom_runner is not None or runtime is not None else evaluate_by_replay(
                            *evaluation_arguments,
                        ))
                    except BaseException:
                        status["replay_status"] = "failed"
                        raise
                    status.update(replay_status="completed", replay_choice=evaluation["choice"])
                    write_stage_report(run, "replay", "Проверка решением задачи", "completed",
                                       f"Выбор: {evaluation['choice']}\n\n{evaluation['summary']}",
                                       links={"Сравнение": run / "replay/decision.json", "Результат": run / "replay/result"})
                    if evaluation["choice"] != "candidate":
                        status["state"] = "rejected"
                        raise RuntimeError(f"Память не обновлена после прогона: {evaluation['summary']}")
            else:
                write_stage_report(run, "replay", "Проверка решением задачи", "not_requested",
                                   "Проверка не запрошена. Принятие памяти основано на ревью.")
            if read_memory(run / "after", stage, layout) != proposal.after:
                raise RuntimeError("Проверяемая копия изменилась; перенос отменён.")
            if replay_context is not None and report_fingerprint(replay_context) != status["report_fingerprint"]:
                raise RuntimeError("Аналитические отчёты изменились во время обработки; перенос отменён.")
            status["snapshot_sha256"] = {
                name: hashlib.sha256(content).hexdigest() for name, content in proposal.after.items()
            }
            status["analysis_sessions"] = []
            if replay_context is not None:
                for manifest_path in sorted(replay_context.glob("session-*/manifest.json")):
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest.get("state") == "completed":
                        status["analysis_sessions"].append({
                            "session_id": manifest.get("session_id", manifest_path.parent.name.removeprefix("session-")),
                            "source": manifest.get("source"),
                        })
            if proposal.after != proposal.before:
                if store is not None:
                    store.publish(proposal.before, proposal.after, run)
                elif layout is not None:
                    publish_memory(target, proposal.before, proposal.after, stage, run, layout)
                else:
                    publish_memory(target, proposal.before, proposal.after, stage, run)
            elif (store.snapshot() if store is not None else read_memory(target, stage, layout)) != proposal.before:
                raise RuntimeError("Действующая память изменилась; результат устарел.")
            status["state"] = "published" if proposal.after != proposal.before else "unchanged"
    except BaseException as error:
        status.setdefault("error", str(error))
        if status["state"] == "running":
            status["state"] = "failed"
        index = run / "report/index.json"
        if index.exists():
            for name, entry in json.loads(index.read_text(encoding="utf-8")).items():
                if entry["state"] == "running":
                    write_stage_report(run, name, entry["title"], "failed", str(error))
        raise
    finally:
        if status["replay_status"] == "not_requested" and not (run / "report/replay.md").exists():
            write_stage_report(run, "replay", "Проверка решением задачи", "not_requested",
                               "Проверка решением задачи не запрошена.")
        if status["replay_status"] == "pending":
            status["replay_status"] = "not_run"
            write_stage_report(run, "replay", "Проверка решением задачи", "not_run",
                               "Прогон не запускался: обработка остановлена до решения о его выполнении.")
        (run / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        applied = status["state"] in ("published", "unchanged")
        write_stage_report(run, "publication", "Принятие памяти", status["state"],
                           ("Память принята. Область: " + str(target) if applied else
                            "Память не принята. " + status.get("error", "")),
                           proposal.before if applied else None, proposal.after if applied else None,
                           {"Состояние запуска": run / "status.json", "Память до изменений": run / "before",
                            "Память после ревью": run / "after"})
