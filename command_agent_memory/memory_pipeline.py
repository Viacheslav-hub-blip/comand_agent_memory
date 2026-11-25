"""Последовательность обработки внешней истории и обновления подключённой памяти.

Функции: accepted_run находит неизменный принятый результат;
personal_result читает журнал; run_personal обновляет память участника; run_local_team
объединяет и доставляет локальную память; run_pipeline выполняет выбранный режим.
Анализ истории, модельные вызовы и сохранение документов выполняют отдельные группы.
"""
import hashlib
import importlib
import json
from pathlib import Path

from command_agent_memory.application_config import read_configuration, validate_configuration
from command_agent_memory.model_execution.stage_runner import StageRuntime
from command_agent_memory.connections import validate_users, team_connection
from command_agent_memory.history_analysis.session_analysis import analyze_source
from command_agent_memory.history_analysis.analysis_reports import read_reports, report_fingerprint
from command_agent_memory.memory_update.personal_memory_update import consolidate_with_review, MemoryRejected
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.file_storage import FileMemoryStore
from command_agent_memory.team_memory_delivery.local_copy import sync_team_memory
from command_agent_memory.run_reports import create_run_directory
from command_agent_memory.skill_resources import get_skill_path
from command_agent_memory.worker_agents import ReplayFactory


def accepted_run(user: dict, fingerprint: str, runtime: StageRuntime) -> Path | None:
    """Принимает подключение и версии входов; возвращает неизменный принятый запуск либо None."""
    current = {name: hashlib.sha256(value).hexdigest() for name, value in user["store"].snapshot().items()}
    for path in sorted((user["workspace"] / "consolidation-runs").glob("*/personal/status.json"), reverse=True):
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("state") in ("published", "unchanged") and state.get("user_id") == user["user_id"]
                and state.get("target") == str(user["target"].resolve())
                and state.get("report_fingerprint") == fingerprint and state.get("snapshot_sha256") == current
                and state.get("configuration_sha256") == runtime.fingerprint()
                and state.get("memory_store_identity") == user["store"].identity
                and state.get("memory_layout") == {"personal": list(user["layout"].personal),
                                                   "knowledge": list(user["layout"].knowledge)}):
            after = FileMemoryStore(path.parent / "after", user["layout"].entries).snapshot()
            if {name: hashlib.sha256(value).hexdigest() for name, value in after.items()} == current:
                return path.parent
    return None


def personal_result(user_id: str, state: str, run: Path | None = None,
                    error: Exception | None = None) -> dict:
    """Принимает итог участника и путь запуска; возвращает статус и сведения о replay."""
    result = {"user_id": user_id, "state": state}
    if error is not None:
        result["error"] = str(error)
    if run is None:
        return result
    result["run"] = str(run)
    journal = run / "status.json"
    try:
        status = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            raise ValueError("Нужен объект JSON.")
    except (OSError, ValueError) as journal_error:
        if error is None:
            raise
        result["journal_error"] = str(journal_error)
        return result
    result.update(status_file=str(journal), replay_status=status.get("replay_status"),
                  replay_choice=status.get("replay_choice"))
    if state == "completed":
        result["state"] = status["state"]
    return result


def run_personal(user: dict, runtime: StageRuntime,
                 evaluate_by_replay: bool) -> tuple[dict, Path | None, Exception | None]:
    """Принимает участника, модели и режим; возвращает итог, принятый запуск и причину отказа."""
    user_id, workspace = user["user_id"], user["workspace"]
    run = None
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with memory_lock(workspace):
            reports = analyze_source(user, runtime)
            if not reports.is_dir():
                return personal_result(user_id, "no_sessions"), None, None
            fingerprint = report_fingerprint(reports)
            prior = None if evaluate_by_replay else accepted_run(user, fingerprint, runtime)
            if prior is not None:
                return personal_result(user_id, "reused", prior), prior, None
            context, procedures, _ = read_reports(reports, user_id=user_id)
            run = create_run_directory(workspace / "consolidation-runs") / "personal"
            consolidate_with_review(
                user["target"], "personal", context, get_skill_path("personal-memory-consolidation"),
                "", "configured", run, knowledge_materials=procedures, user_id=user_id,
                replay_requested=evaluate_by_replay, replay_scenario=user["scenario"] if evaluate_by_replay else None,
                replay_context=reports, layout=user["layout"], runtime=runtime, store=user["store"],
            )
            return personal_result(user_id, "completed", run), run, None
    except Exception as error:
        state = "rejected" if isinstance(error, MemoryRejected) else "failed"
        return personal_result(user_id, state, run, error), None, error


def run_local_team(team_root: Path, workspaces: list[Path], accepted: list[Path],
                   runtime: StageRuntime, result: dict) -> None:
    """Принимает команду, получателей и принятые запуски; обновляет итог и доставляет память."""
    from command_agent_memory.memory_update.team_memory_update import consolidate_team_memory
    from command_agent_memory.memory_storage.memory_snapshots import read_memory

    if not accepted:
        result["state"] = "skipped"
        return
    team_runs = accepted[0].parents[2] / "team-runs"
    current = {name: hashlib.sha256(value).hexdigest()
               for name, value in read_memory(team_root, "knowledge").items()}
    for status_path in sorted(team_runs.glob("*/knowledge/status.json"), reverse=True):
        state = json.loads(status_path.read_text(encoding="utf-8"))
        if (state.get("state") in ("published", "unchanged") and state.get("target") == str(team_root.resolve())
                and state.get("accepted_runs") == [str(value.resolve()) for value in accepted]
                and state.get("snapshot_sha256") == current
                and state.get("configuration_sha256") == runtime.fingerprint()):
            result.update(state="reused", status_file=str(status_path))
            break
    else:
        run = create_run_directory(team_runs) / "knowledge"
        result["status_file"] = str(run / "status.json")
        state = consolidate_team_memory(team_root, accepted, run, "", "configured", runtime.runner("team"),
                                        configured=True, configuration_sha256=runtime.fingerprint(), runtime=runtime)
        runtime.save(run / "configuration")
        result["state"] = state["state"]
    result["deliveries"] = []
    for workspace in workspaces:
        delivery_run = create_run_directory(team_runs) / "delivery"
        sync_team_memory(team_root, workspace, delivery_run)
        result["deliveries"].append(str(delivery_run / "status.json"))
        print(f"Командная память подключена: {workspace / 'team_knowledge'}; журнал: {delivery_run}")


def run_pipeline(path: Path, *, validate_only: bool = False, evaluate_by_replay: bool = False,
                 team_mode: str | None = None, raise_on_failure: bool = True,
                 configuration: dict | None = None, replay_factory: ReplayFactory | None = None) -> dict:
    """Принимает JSON и режим; возвращает результаты участников и командного этапа.

    Валидация проверяет входы до записи памяти. При ошибке отдельного цикла
    остальные участники продолжают работу. raise_on_failure сохраняет прежний
    способ сообщать ошибку через исключение после завершения остальных этапов.
    configuration позволяет передать готовые настройки. Относительные пути
    по-прежнему отсчитываются от родительского каталога path.
    replay_factory(workspace, layout) создаёт рабочую сессию и включает обязательный replay.
    """
    if team_mode not in (None, "process", "sync", "status"):
        raise ValueError("team_mode должен быть process, sync, status или None.")
    if team_mode is not None and evaluate_by_replay:
        raise ValueError("Режим Git-команды несовместим с реплеем личной памяти.")
    if replay_factory is not None and (not callable(replay_factory) or not evaluate_by_replay):
        raise ValueError("replay_factory требует режима диалоговой проверки.")
    path = path.absolute()
    if configuration is None:
        configuration = read_configuration(path, team_only=team_mode is not None)
    else:
        configuration = validate_configuration(configuration, team_only=team_mode is not None)
    extensions = configuration.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(name, str) or not name for name in extensions):
        raise ValueError("extensions должен содержать имена Python-модулей.")
    for name in extensions:
        importlib.import_module(name)
    runtime = StageRuntime(configuration, path.parent, replay_factory=replay_factory)
    users = [] if team_mode == "status" else validate_users(configuration, path.parent, recover=not validate_only)
    stages = set()
    if not team_mode:
        stages.update({"context", "procedures", "review"})
        if any(user["layout"].personal for user in users):
            stages.add("consolidation")
        if any(user["layout"].knowledge for user in users):
            stages.add("knowledge")
        if evaluate_by_replay:
            stages.update({"replay_tester", "evaluation"})
    team = configuration.get("team")
    git_team, team_root, team_workspaces = team_connection(team, path.parent, users, recover=not validate_only)
    if team_mode == "process" or team_root is not None:
        stages.add("team")
    if team_mode and git_team is None:
        raise ValueError("Командные режимы process и sync требуют team.type=git.")
    runtime.validate(stages, load_providers=not validate_only)
    if evaluate_by_replay:
        from command_agent_memory.memory_replay.replay_scenario import check_replay_data
        from command_agent_memory.worker_agents import validate_agent_spec

        for user in users:
            assessment = check_replay_data(user["scenario"])
            if assessment["decision"] != "run":
                raise ValueError(f"Replay для {user['user_id']}: {assessment['summary']}.")
            if replay_factory is None:
                validate_agent_spec(user["scenario"].agent)
    result = {"state": "completed", "users": [], "team": None}
    if validate_only:
        result["state"] = "validated"
        if git_team:
            result["team"] = {"state": "validated", "mode": team_mode or "contribute"}
        for user in users:
            count_sessions = 0
            for session in user["sessions"]:
                count = sum(1 for _ in user["source"].iter_events(session))
                if not count:
                    raise ValueError(f"Пустая сессия {session['id']}")
                print(f"{user['user_id']} / {session['id']}: событий {count}")
                count_sessions += 1
            result["users"].append({"user_id": user["user_id"], "state": "validated", "sessions": count_sessions})
        print("Подключения проверены; модели не вызывались, файлы не создавались.")
        return result
    accepted, failures = [], []
    if not team_mode:
        for user in users:
            status, run, error = run_personal(user, runtime, evaluate_by_replay)
            result["users"].append(status)
            if run is not None:
                accepted.append(run)
            if error is not None:
                failures.append(error)
            details = status.get("error") or status.get("run", "")
            print(f"{user['user_id']}: {status['state']} {details}".rstrip())
    if team is not None:
        result["team"] = {}
        try:
            if git_team is not None:
                from command_agent_memory.team_memory_delivery.git_team import run_git_team

                from command_agent_memory.memory_update.team_memory_update import git_team_proposer

                propose = git_team_proposer(runtime) if team_mode == "process" else None
                run_git_team(git_team, team_mode or "contribute", accepted, team_workspaces,
                             result["team"], propose=propose)
            else:
                run_local_team(team_root, team_workspaces, accepted, runtime, result["team"])
        except Exception as error:
            from command_agent_memory.memory_update.team_memory_update import TeamMemoryRejected

            failures.append(error)
            team_state = "rejected" if isinstance(error, TeamMemoryRejected) else "failed"
            result["team"].update(state=team_state, error=str(error))
        if git_team is not None:
            print(f"Командная память: {result['team']['state']}.")
    if failures:
        result["state"] = "failed"
        if raise_on_failure:
            raise failures[0]
    return result
