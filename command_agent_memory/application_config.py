"""Настройки экземпляра приложения и разрешение пользовательских путей.

Функции: read_local_settings — старые локальные настройки; resolve_path — путь;
read_configuration — чтение JSON. StageSettings хранит параметры модели этапа.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import runpy

from command_agent_memory.skill_resources import ROOT


STAGES = frozenset({"context", "procedures", "consolidation", "knowledge", "review",
                    "replay_agent", "replay_tester", "evaluation", "team"})


def read_local_settings() -> dict:
    """Без входов возвращает значения local_settings/agent_settings.py либо пустой словарь, если файла нет.

    Файл остаётся необязательным доверенным Python-кодом локального проекта.
    Секреты и полученные значения не выводятся и не копируются в журналы.
    """
    path = ROOT / "local_settings/agent_settings.py"
    return runpy.run_path(str(path)) if path.is_file() else {}


def resolve_path(base: Path, value: str) -> Path:
    """Принимает базовый каталог и строку пути; возвращает абсолютный путь."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Нужен непустой путь.")
    return Path(os.path.abspath(base / Path(value).expanduser()))


def read_configuration(path: Path) -> dict:
    """Принимает путь JSON; возвращает объект конфигурации, отклоняя неизвестные поля."""
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = {"defaults", "stages", "wishes_file", "users", "team", "extensions"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Неизвестные поля конфигурации подключения.")
    if not isinstance(value.get("users"), list) or not value["users"]:
        raise ValueError("Нужен непустой список users.")
    return value


@dataclass(frozen=True)
class StageSettings:
    """Принимает провайдера, модель, путь CLI и бюджеты; хранит настройки одного этапа."""

    provider: str
    model: str
    executable: str | None = None
    timeout_seconds: int | None = None
    max_model_calls: int | None = None
