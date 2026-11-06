"""Подключение штатных агентов для обычной работы и повторного решения задач.

WorkerSession хранит агент, профиль и освобождение ресурсов; close завершает сессию.
Функции: validate_agent_spec проверяет подключение; load_worker_session вызывает
фабрику и проверяет профиль; profile_fingerprint возвращает версию агента;
require_same_profile проверяет сопоставимость двух сессий.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from command_agent_memory.memory_storage.memory_layout import MemoryLayout


@dataclass
class WorkerSession:
    """Принимает агент со stream, несекретный профиль и cleanup; хранит отдельную рабочую сессию."""

    agent: Any
    profile: dict
    cleanup: Callable[[], None] | None = None

    def close(self) -> None:
        """Без входов освобождает ресурсы фабрики один раз; возвращает None."""
        cleanup, self.cleanup = self.cleanup, None
        if cleanup is not None:
            cleanup()


def validate_agent_spec(specification: dict | None) -> None:
    """Принимает подключение; проверяет factory/options без импорта и запуска, возвращает None."""
    if (not isinstance(specification, dict) or set(specification) - {"factory", "options"}
            or not isinstance(specification.get("factory"), str)
            or specification["factory"].count(":") != 1
            or not all(part and all(piece.isidentifier() for piece in part.split("."))
                       for part in specification["factory"].split(":"))
            or not isinstance(specification.get("options", {}), dict)):
        raise ValueError("Для replay нужен agent: {factory: 'module:function', options: {...}}.")
    json.dumps(specification, ensure_ascii=False, sort_keys=True)


def profile_fingerprint(profile: dict) -> str:
    """Принимает несекретный профиль; возвращает SHA-256 модели, инструментов и условий исполнения."""
    return hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def load_worker_session(specification: dict, workspace: Path,
                        memory_layout: MemoryLayout | None = None) -> WorkerSession:
    """Принимает подключение, копию проекта и layout; возвращает штатную сессию фабрики.

    Фабрика принимает workspace, options и memory_layout. Она возвращает WorkerSession,
    сохраняет штатные инструменты и сама привязывает внешние ресурсы к отдельному
    состоянию опыта. Импортируемый Python-код доверенный. Профиль не содержит секретов.
    """
    validate_agent_spec(specification)
    module, name = specification["factory"].split(":")
    factory = importlib.import_module(module)
    for part in name.split("."):
        factory = getattr(factory, part)
    session = factory(workspace=workspace, options=deepcopy(specification.get("options", {})), memory_layout=memory_layout)
    if not isinstance(session, WorkerSession):
        raise TypeError("Фабрика рабочего агента должна вернуть WorkerSession.")
    try:
        profile = session.profile
        required = {"model", "model_parameters", "system_prompt_sha256", "tools", "environment", "implementation"}
        if (not callable(getattr(session.agent, "stream", None)) or not isinstance(profile, dict)
                or not required.issubset(profile) or not isinstance(profile["model"], str) or not profile["model"]
                or not isinstance(profile["model_parameters"], dict) or not isinstance(profile["environment"], dict)
                or not isinstance(profile["system_prompt_sha256"], str) or len(profile["system_prompt_sha256"]) != 64
                or not isinstance(profile["tools"], list) or not isinstance(profile["implementation"], str)
                or any(not isinstance(tool, dict) or not {"name", "schema", "implementation"}.issubset(tool)
                       or not isinstance(tool["name"], str) or not isinstance(tool["schema"], dict)
                       or not isinstance(tool["implementation"], str) for tool in profile["tools"])):
            raise ValueError("Фабрика вернула неполный профиль рабочего агента.")
        names = [tool["name"] for tool in profile["tools"]]
        if len(set(names)) != len(names):
            raise ValueError("Имена инструментов рабочего агента должны быть уникальными.")
        try:
            source = inspect.getsourcefile(factory)
        except TypeError:
            source = None
        session.profile = {**profile, "factory": specification["factory"],
                           "factory_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None,
                           "options_sha256": profile_fingerprint(specification.get("options", {}))}
        profile_fingerprint(session.profile)
    except BaseException:
        session.close()
        raise
    return session


def require_same_profile(before: WorkerSession, after: WorkerSession) -> str:
    """Принимает две сессии; возвращает общий отпечаток либо отклоняет несопоставимые профили."""
    fingerprint = profile_fingerprint(before.profile)
    if fingerprint != profile_fingerprint(after.profile):
        raise RuntimeError("Профили рабочего агента различаются: модель, инструменты или условия запуска изменились.")
    if before.agent is after.agent:
        raise RuntimeError("Фабрика должна создавать независимый экземпляр агента для каждого прогона.")
    return fingerprint
