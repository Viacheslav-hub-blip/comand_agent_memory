"""Проверка общих подключений CLI и автономных скиллов.

Функции: validate_users проверяет участников и области памяти, validate_team_workspaces
проверяет каталоги доставки, team_connection возвращает параметры командного хранилища.
"""
from pathlib import Path

from command_agent_memory.application_config import resolve_path
from command_agent_memory.history_sources.conversation_history import load_history_source
from command_agent_memory.history_sources.project_sessions import resolve_project_source
from command_agent_memory.memory_storage.file_storage import FileMemoryStore, checked_path
from command_agent_memory.memory_storage.memory_layout import MemoryLayout
from command_agent_memory.memory_storage.storage_registry import load_memory_store


def validate_users(configuration: dict, base: Path, *, recover: bool = False) -> list[dict]:
    """Принимает конфигурацию и каталог; возвращает проверенные подключения и источники.

    Доступ к истории проверяется до первого изменения файлов. Рабочие журналы
    не должны пересекаться с выбранной памятью или памятью другого пользователя.
    recover разрешает восстановление прерванной файловой публикации до чтения.
    """
    result, identities = [], set()
    for item in configuration["users"]:
        if not isinstance(item, dict) or set(item) - {"user_id", "workspace", "source", "memory", "replay_scenario"}:
            raise ValueError("Неизвестные поля подключения пользователя.")
        identity = item.get("user_id")
        if not isinstance(identity, str) or not identity or not all(c.isalnum() or c in "_-" for c in identity):
            raise ValueError("user_id должен содержать буквы, цифры, _ или -.")
        if identity in identities:
            raise ValueError(f"Повтор user_id: {identity}")
        identities.add(identity)
        workspace = resolve_path(base, item.get("workspace"))
        memory = item.get("memory")
        if not isinstance(memory, dict):
            raise ValueError("memory должен содержать настройки хранилища и списки personal/knowledge.")
        groups = [memory.get(name, []) for name in ("personal", "knowledge")]
        if any(not isinstance(group, list) for group in groups):
            raise ValueError("personal и knowledge должны быть списками путей.")
        layout = MemoryLayout(*(tuple(group) for group in groups))
        store = load_memory_store(memory, base, layout)
        target = (resolve_path(base, memory.get("root")) if memory.get("type", "files") == "files"
                  else workspace / ".memory-store")
        if isinstance(store, FileMemoryStore):
            store.snapshot(recover=recover)
        else:
            store.snapshot()
        paths = list(store.protected_paths)
        if workspace == Path(workspace.anchor) or any(
                path.is_relative_to(workspace) or workspace.is_relative_to(path) for path in paths):
            raise ValueError("Журналы и выбранные файлы памяти должны находиться раздельно.")
        for previous in result:
            if previous["store"].identity == store.identity and not paths:
                raise ValueError("Пользователи не должны использовать одну область памяти.")
            previous_paths = previous["store"].protected_paths
            all_pairs = [(workspace, previous["workspace"]),
                         *[(path, previous["workspace"]) for path in paths],
                         *[(workspace, path) for path in previous_paths],
                         *[(left, right) for left in paths for right in previous_paths]]
            if any(left.is_relative_to(right) or right.is_relative_to(left) for left, right in all_pairs):
                raise ValueError("Рабочие области и память пользователей пересекаются.")
        options = resolve_project_source(item.get("source"), base)
        source = load_history_source(options, base)
        sessions = source.list_sessions()
        selected = options.get("session_ids")
        projects = options.get("projects")
        if selected is not None:
            if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
                raise ValueError("session_ids должен содержать строки.")
            missing = set(selected) - {session["id"] for session in sessions}
            if missing:
                raise ValueError(f"Не найдены сессии: {sorted(missing)}")
            sessions = [session for session in sessions if session["id"] in selected]
        if projects is not None:
            if not isinstance(projects, list) or any(not isinstance(value, str) for value in projects):
                raise ValueError("projects должен содержать строки.")
            missing = {value.casefold() for value in projects} - {session.get("project", "").casefold() for session in sessions}
            if missing:
                raise ValueError(f"Не найдены проекты: {sorted(missing)}")
            sessions = [session for session in sessions if session.get("project", "").casefold()
                        in {value.casefold() for value in projects}]
        limit = options.get("limit")
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise ValueError("limit должен быть положительным целым числом.")
            sessions = sessions[:limit]
        if len({session["id"] for session in sessions}) != len(sessions):
            raise ValueError("Источник вернул повторяющиеся идентификаторы сессий.")
        scenario = None
        if item.get("replay_scenario"):
            from command_agent_memory.memory_replay.replay_scenario import load_replay_scenario

            scenario = load_replay_scenario(resolve_path(base, item["replay_scenario"]),
                                            allow_incomplete=True, allowed_root=base)
        result.append({"user_id": identity, "workspace": workspace, "target": target,
                       "layout": layout, "store": store, "source": source, "sessions": sessions,
                       "source_options": item["source"], "scenario": scenario})
    return result


def validate_team_workspaces(team: dict, base: Path, users: list[dict], *,
                             protected_root: Path, recover: bool = False) -> list[Path]:
    """Принимает настройки, участников и защищённый корень; возвращает получателей.

    Проверяет существование каталогов, ссылки и пересечения до модельных вызовов.
    Записываемый team_knowledge/ не должен пересекаться с источниками истории,
    личной памятью, журналами, конфигурацией или командным источником. Файлы не создаёт.
    При recover=True допускается восстановление прежней публикации получателя.
    """
    values = team.get("workspaces", [])
    if not isinstance(values, list):
        raise ValueError("team.workspaces должен быть списком каталогов.")
    protected = [protected_root]
    for user in users:
        protected.extend([user["workspace"], *user["store"].protected_paths])
        source = user["source_options"]
        if isinstance(source.get("path"), str):
            protected.append(resolve_path(base, source["path"]))
    result, targets = [], []
    for value in values:
        workspace = resolve_path(base, value)
        if not workspace.is_dir() or workspace == Path(workspace.anchor):
            raise ValueError("Нужна существующая рабочая область получателя.")
        target = checked_path(workspace, "team_knowledge").resolve()
        if target == base.resolve() or base.resolve().is_relative_to(target):
            raise ValueError("Копия командной памяти не должна содержать конфигурацию.")
        if any(target.is_relative_to(path.resolve()) or path.resolve().is_relative_to(target)
               for path in [*protected, *targets]):
            raise ValueError("Копия командной памяти пересекается с защищёнными путями.")
        FileMemoryStore(workspace, ("team_knowledge",)).snapshot(recover=recover)
        result.append(workspace)
        targets.append(target)
    return result


def team_connection(team: dict | None, base: Path, users: list[dict], *, recover: bool = False):
    """Принимает настройки команды и участников; возвращает Git-подключение, каталог и получателей."""
    git_team = team_workspaces = team_root = None
    if isinstance(team, dict) and team.get("type") == "git":
        from command_agent_memory.team_memory_delivery.git_team import GitTeamConfig

        git_team = GitTeamConfig.from_json(team, base, users)
        team_workspaces = validate_team_workspaces(
            team, base, users, protected_root=git_team.cache_dir, recover=recover)
    elif team is not None:
        if not isinstance(team, dict) or "root" not in team or set(team) - {"root", "workspaces"}:
            raise ValueError("team должен содержать root и необязательный workspaces либо type=git.")
        team_root = resolve_path(base, team["root"])
        if team_root == Path(team_root.anchor):
            raise ValueError("Корень файловой системы нельзя использовать как командную память.")
        for user in users:
            protected = [user["workspace"], *user["store"].protected_paths]
            if any(team_root.is_relative_to(value) or value.is_relative_to(team_root) for value in protected):
                raise ValueError("Командная память пересекается с журналами или личной памятью.")
        team_workspaces = validate_team_workspaces(
            team, base, users, protected_root=team_root, recover=recover)
    return git_team, team_root, team_workspaces
