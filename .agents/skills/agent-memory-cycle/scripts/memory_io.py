"""Технические операции над историей и памятью для автономных Agent Skills.

Функции: hashes вычисляет отпечатки, configuration_digest связывает конфиг с инструкциями,
stage_evidence проверяет журналы этапов, read_json читает документ, save_json сохраняет
его атомарно, prepare создаёт изолированные материалы, seal фиксирует результат ревью,
publish переносит проверенные байты, operate выбирает одну операцию, main обслуживает CLI.
"""
import argparse
from dataclasses import asdict
import hashlib
import importlib
import json
from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS / "io_lib" if (SCRIPTS / "io_lib").is_dir() else SCRIPTS.parents[3]))
sys.path.insert(0, str(SCRIPTS))
from command_agent_memory.application_config import read_configuration, resolve_path
from command_agent_memory.connections import team_connection, validate_users
from command_agent_memory.history_sources.conversation_history import export_events
from command_agent_memory.history_sources.project_sessions import DEFAULT_RUNS, protect_local_runs
from command_agent_memory.memory_replay.replay_scenario import check_replay_data
from command_agent_memory.memory_storage.file_storage import checked_path
from command_agent_memory.memory_storage.memory_locking import memory_lock
from command_agent_memory.memory_storage.memory_snapshots import read_memory, write_snapshot
from command_agent_memory.run_reports import create_run_directory
from command_agent_memory.team_memory_delivery.contributions import write_atomic


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    """Принимает документы снимка и возвращает SHA-256 каждого файла."""
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def read_json(path: Path) -> dict:
    """Принимает путь к документу и возвращает JSON-объект."""
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Ожидался JSON-объект в {path}")
    return value


def save_json(path: Path, value: dict) -> None:
    """Принимает путь и объект, атомарно сохраняет UTF-8 JSON и возвращает None."""
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def configuration_digest(config: Path, settings: dict) -> str:
    """Принимает файл настроек и разобранные поля; возвращает версию конфига, пожеланий и скиллов."""
    router = SCRIPTS.parent
    installed = (SCRIPTS / "io_lib").is_dir()
    stages = router.parent if installed else SCRIPTS.parents[3] / "command_agent_memory/skills"
    documents = {"config": hashlib.sha256(config.read_bytes()).hexdigest()}
    if settings.get("wishes_file"):
        wishes = resolve_path(config.parent, settings["wishes_file"])
        documents["wishes"] = hashlib.sha256(wishes.read_bytes()).hexdigest()
    for name in ("context-analysis", "procedure-analysis", "personal-memory-consolidation",
                 "knowledge-consolidation", "memory-review", "replay-tester", "replay-evaluation",
                 "team-memory-consolidation", router.name):
        directory = router if name == router.name else stages / name
        resources = [directory / "SKILL.md", *(directory / "references").rglob("*.md")]
        for path in resources:
            documents[name + "/" + path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()


def stage_evidence(run: Path, state: dict) -> dict:
    """Принимает журнал и состав запуска; проверяет ID сессий, исходные файлы и отчёты обязательных этапов."""
    records = read_json(run / "stages.json")
    required = {"review"}
    for item in state["history_inputs"]:
        path = checked_path(run / "history", item["file"])
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Исходная история изменилась после подготовки.")
        required.update(f"{stage}-{path.stem}" for stage in ("context", "procedures"))
    for group, stage in (("personal", "consolidation"), ("knowledge", "knowledge")):
        if state["memory_layout"][group]:
            required.add(stage)
    if required != set(records):
        raise ValueError(f"Для этого запуска нужны журналы этапов {sorted(required)}.")
    versions, sessions = {}, set()
    for stage, row in records.items():
        session = row["session_id"]
        if not isinstance(session, str) or not session.strip() or session in sessions:
            raise ValueError("Каждому этапу нужна отдельная штатная сессия.")
        if stage == "review" and row["report"] != "review.json":
            raise ValueError("Запись этапа review должна ссылаться на review.json.")
        path = checked_path(run, row["report"])
        content = path.read_bytes()
        if not content.strip():
            raise ValueError(f"Пустой отчёт этапа {stage}.")
        versions[stage] = {"session_id": session, "sha256": hashlib.sha256(content).hexdigest()}
        sessions.add(session)
    return versions


def prepare(user: dict, config_hash: str, replay: bool, profile: str | None) -> dict:
    """Принимает подключение, версию настроек и режим replay; возвращает пути материалов одной версии."""
    if not user["sessions"]:
        return {"state": "no_sessions", "user_id": user["user_id"]}
    if user["workspace"] == user["target"] / DEFAULT_RUNS:
        protect_local_runs(user["target"])
    run = create_run_directory(user["workspace"] / "skill-runs") / "personal"
    before = user["store"].snapshot()
    write_snapshot(run / "before", before)
    write_snapshot(run / "draft", before)
    history = run / "history"
    history.mkdir()
    sessions = []
    for index, session in enumerate(user["sessions"], 1):
        filename = f"{index:06d}.jsonl"
        path = history / filename
        if not export_events(user["source"], session, path):
            raise ValueError(f"Пустая сессия {session['id']}")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        sessions.append({"session_id": session["id"], "source": user["source_options"]["type"],
                         "file": filename, "sha256": checksum})
    state = {"stage": "personal", "state": "prepared", "user_id": user["user_id"],
             "configuration_sha256": config_hash, "profile": profile, "memory_layout": asdict(user["layout"]),
             "target": str(user["target"]), "memory_store_identity": user["store"].identity,
             "before_sha256": hashes(before), "history_inputs": sessions, "replay_requested": replay,
             "analysis_sessions": [{k: item[k] for k in ("session_id", "source")} for item in sessions]}
    save_json(run / "status.json", state)
    if profile and not replay:
        for status in sorted(run.parents[1].glob("*/personal/status.json"), reverse=True):
            prior = read_json(status)
            keys = ("user_id", "configuration_sha256", "memory_store_identity", "profile", "history_inputs")
            if (prior.get("state") in {"published", "unchanged"} and all(prior.get(k) == state[k] for k in keys)
                    and prior["snapshot_sha256"] == state["before_sha256"]
                    and hashes(read_memory(status.parent / "after", "knowledge")) == state["before_sha256"]
                    and stage_evidence(status.parent, prior) == prior["stage_evidence"]):
                save_json(run / "status.json", {**state, "state": "reused", "accepted_run": str(status.parent)})
                return {"state": "reused", "run": str(status.parent), "user_id": user["user_id"]}
    return {"run": str(run), **state}


def seal(run: Path, user: dict, state: dict) -> dict:
    """Принимает журнал и подключение; фиксирует только проверенную область и решение ревьюера."""
    evidence = stage_evidence(run, state)
    review = read_json(run / "review.json")
    if (set(review) != {"decision", "summary", "findings"} or review["decision"] != "accepted"
            or review["findings"] != [] or not isinstance(review["summary"], str) or not review["summary"].strip()):
        raise ValueError("Публикации требуется принятое ревью без открытых замечаний.")
    draft = read_memory(run / "draft", "knowledge")
    if any(not user["layout"].allows(name) for name in draft):
        raise ValueError("Черновик содержит файлы вне области памяти.")
    if (run / "after").exists():
        if read_memory(run / "after", "knowledge") != draft:
            raise ValueError("Сохранённый снимок ревью отличается от черновика.")
    else:
        write_snapshot(run / "after", draft)
    state.update(state="reviewed", snapshot_sha256=hashes(draft), stage_evidence=evidence,
                 review_sha256=hashlib.sha256((run / "review.json").read_bytes()).hexdigest())
    save_json(run / "status.json", state)
    return state


def publish(run: Path, user: dict, state: dict) -> dict:
    """Принимает зафиксированный снимок и решение replay; возвращает итог транзакционной публикации."""
    if state["state"] not in {"reviewed", "published", "unchanged"}:
        raise ValueError("Сначала зафиксируйте проверенный снимок командой seal.")
    before = read_memory(run / "before", "knowledge")
    after = read_memory(run / "after", "knowledge")
    if (hashes(before) != state["before_sha256"] or hashes(after) != state["snapshot_sha256"]
            or stage_evidence(run, state) != state["stage_evidence"]):
        raise ValueError("Снимок изменился после подготовки или ревью.")
    if hashlib.sha256((run / "review.json").read_bytes()).hexdigest() != state["review_sha256"]:
        raise ValueError("Решение ревью изменилось после фиксации.")
    if state["replay_requested"]:
        from replay_check import verify_replay

        verify_replay(run, user["scenario"], before, after)
        state.update(replay_status="completed", replay_choice="candidate")
    else:
        state["replay_status"] = "not_requested"
    store = user["store"]
    current = store.snapshot()
    if current != after:
        if state["state"] != "reviewed" or current != before:
            raise RuntimeError("Действующая память изменилась; требуется новое предложение.")
        store.publish(before, after, run)
    state["state"] = "unchanged" if before == after else "published"
    save_json(run / "status.json", state)
    return state


def operate(args: argparse.Namespace) -> dict:
    """Принимает параметры одной операции; возвращает проверенные материалы либо результат записи."""
    if args.action == "discover":
        from command_agent_memory.history_sources.project_sessions import default_configuration

        project = args.project.resolve(strict=True)
        settings = default_configuration(project, args.host, args.source, args.history, args.session)
        user, = settings["users"]
        user["workspace"] = str((project / user["workspace"]).resolve())
        user["memory"]["root"] = str(project)
        return settings
    config = args.config.resolve(strict=True)
    settings = read_configuration(config, team_only=args.action.startswith("team-"))
    config_hash = configuration_digest(config, settings)
    for module in settings.get("extensions", []):
        importlib.import_module(module)
    users = [] if args.action == "team-status" else validate_users(settings, config.parent, recover=args.action != "check")
    git, local, recipients = team_connection(settings.get("team"), config.parent, users,
                                             recover=args.action != "check")
    if args.action.startswith("team-"):
        from team_io import team_operation

        return team_operation(args, git, local, recipients)
    selected = [u for u in users if not args.user or u["user_id"] == args.user]
    if not selected:
        raise ValueError("Участник не найден в конфигурации.")
    if args.replay:
        for user in selected:
            assessment = check_replay_data(user["scenario"])
            if assessment["decision"] != "run":
                raise ValueError(assessment["summary"])
    if args.action == "check":
        counts = {}
        for user in selected:
            for session in user["sessions"]:
                if not sum(1 for _ in user["source"].iter_events(session)):
                    raise ValueError(f"Пустая сессия {session['id']}")
            counts[user["user_id"]] = len(user["sessions"])
        return {"state": "validated", "sessions": counts}
    if len(selected) != 1:
        raise ValueError("Для работы с одним снимком выберите участника через --user.")
    user, = selected
    if args.action == "prepare":
        return prepare(user, config_hash, args.replay, args.profile)
    run = args.run.resolve(strict=True)
    parent = user["workspace"] / "skill-runs"
    if run.name != "personal" or run.parent.parent != parent.resolve():
        raise ValueError("Запуск не принадлежит рабочей области участника.")
    checked_path(parent, run.relative_to(parent).as_posix())
    with memory_lock(run):
        state = read_json(run / "status.json")
        if (state["configuration_sha256"] != config_hash or state["user_id"] != user["user_id"]
                or state["memory_store_identity"] != user["store"].identity):
            raise ValueError("Настройки или владелец запуска изменились.")
        if args.action == "seal":
            if state["state"] != "prepared":
                raise ValueError("Зафиксировать можно только подготовленный черновик.")
            return seal(run, user, state)
        if args.action == "replay-inputs":
            from replay_check import prepare_replay

            return prepare_replay(run, user, state)
        return publish(run, user, state)


def main() -> int:
    """Читает аргументы одной операции ввода-вывода; печатает JSON и возвращает код завершения."""
    parser = argparse.ArgumentParser(description="Операции с данными автономных скиллов")
    parser.add_argument("action", choices=("discover", "check", "prepare", "seal", "replay-inputs", "publish", "team-input",
                                          "team-apply", "team-submit", "team-sync", "team-status"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--host", default="codex")
    parser.add_argument("--source", choices=("jsonl", "codex", "claude"))
    parser.add_argument("--history", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--user")
    parser.add_argument("--profile", help="Стабильный идентификатор модели и инструментов для повторного запуска.")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--accepted", type=Path, nargs="+", default=[])
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--contribution")
    args = parser.parse_args()
    if args.action == "discover" and args.project is None or args.action != "discover" and args.config is None:
        parser.error("Для discover нужен --project, для операций с памятью нужен --config.")
    if args.action in {"seal", "replay-inputs", "publish"} and args.run is None:
        parser.error("Нужен --run")
    try:
        print(json.dumps(operate(args), ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({"state": "failed", "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
