"""Настройки моделей этапов и единая сборка пользовательских инструкций.

Классы: StageSettings — провайдер и модель; StageRuntime — вызовы и журналы.
Функции: read_configuration — загрузка JSON; resolve_path — разрешение пути;
build_prompt — добавление пожеланий к каждому запросу.
read_local_settings — чтение необязательного local_settings/agent_settings.py.
Методы StageRuntime: __init__, validate, adapter, fingerprint, save, run, runner;
вложенная invoke связывает модель этапа с общим контрактом Runner.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import runpy

from command_agent_memory.model_providers import load_model_adapter
from command_agent_memory.project_paths import ROOT


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


def build_prompt(prompt: str, wishes: str) -> str:
    """Принимает задание и пожелания; возвращает prompt с отдельным разделом пожеланий."""
    if not wishes.strip():
        return prompt
    return "Пожелания пользователя к работе на каждом этапе:\n" + wishes + "\n\nЗадание этапа:\n" + prompt


@dataclass(frozen=True)
class StageSettings:
    """Принимает провайдера, модель и необязательный путь CLI; хранит настройки этапа."""

    provider: str
    model: str
    executable: str | None = None


class StageRuntime:
    """Принимает конфигурацию и базовый каталог; хранит настройки и снимок пожеланий.

    Провайдер загружается лениво. validate проверяет все выбранные этапы до
    первого запроса; runner возвращает исполнитель этапа с контрактом Runner.
    """

    def __init__(self, configuration: dict, base: Path):
        """Принимает настройки; читает пожелания один раз, создаёт проверенные параметры."""
        defaults = configuration.get("defaults", {})
        stages = configuration.get("stages", {})
        if not isinstance(defaults, dict) or not isinstance(stages, dict) or set(stages) - STAGES:
            raise ValueError("Некорректные defaults или имена stages.")
        self.settings = {}
        self.adapters = {}
        self.wishes = ""
        if configuration.get("wishes_file") is not None:
            self.wishes = resolve_path(base, configuration["wishes_file"]).read_text(encoding="utf-8-sig")
        for name in STAGES:
            override = stages.get(name, {})
            if not isinstance(override, dict) or set(override) - {"provider", "model", "executable"}:
                raise ValueError(f"Неизвестные настройки этапа {name}.")
            if set(defaults) - {"provider", "model", "executable"}:
                raise ValueError("Неизвестные настройки defaults.")
            entry = {**defaults, **override}
            if not entry:
                continue
            if any(not isinstance(entry.get(key), str) or not entry[key].strip()
                   for key in ("provider", "model")):
                raise ValueError(f"Для {name} нужны provider и model.")
            executable = entry.get("executable")
            if executable is not None:
                executable = str(resolve_path(base, executable))
            self.settings[name] = StageSettings(entry["provider"], entry["model"], executable)

    def validate(self, stages, *, load_providers: bool = False) -> None:
        """Принимает имена этапов; проверяет настройки и при запросе доступность провайдеров."""
        from command_agent_memory.model_providers import registered_model_adapters

        for name in stages:
            if name not in self.settings:
                raise ValueError(f"Не настроен этап {name}.")
            if self.settings[name].provider not in registered_model_adapters():
                raise ValueError(f"Не зарегистрирован провайдер {self.settings[name].provider}.")
            if load_providers:
                self.adapter(name)

    def adapter(self, stage: str):
        """Принимает имя этапа; возвращает закэшированный адаптер выбранного провайдера."""
        setting = self.settings[stage]
        key = (setting.provider, setting.executable)
        if key not in self.adapters:
            self.adapters[key] = load_model_adapter(
                setting.provider, Path(setting.executable) if setting.executable else None)
        return self.adapters[key]

    def fingerprint(self) -> str:
        """Без входов возвращает SHA-256 настроек и использованного текста пожеланий."""
        value = {"stages": {key: asdict(value) for key, value in self.settings.items()},
                 "wishes": self.wishes}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def save(self, directory: Path) -> None:
        """Принимает каталог журнала; сохраняет настройки и снимок пожеланий без секретов."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "wishes.txt").write_text(self.wishes, encoding="utf-8")
        (directory / "stages.json").write_text(json.dumps(
            {key: asdict(value) for key, value in sorted(self.settings.items())},
            ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self, stage: str, prompt: str, cwd: Path, prefix: Path,
            *, agent: bool = False, writable: bool = False) -> str:
        """Принимает этап, задание и пути; вызывает его модель и возвращает текст ответа."""
        setting = self.settings[stage]
        adapter = self.adapter(stage)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.with_suffix(".stage.json").write_text(json.dumps(
            {"stage": stage, **asdict(setting), "configuration_sha256": self.fingerprint()},
            ensure_ascii=False, indent=2), encoding="utf-8")
        runner = adapter.agent_runner if agent else adapter.chat_runner
        return runner(adapter.executable, setting.model, build_prompt(prompt, self.wishes),
                      cwd, prefix, writable=writable)

    def runner(self, stage: str, *, agent: bool = True):
        """Принимает имя этапа; возвращает Runner с его моделью и общими пожеланиями."""
        def invoke(executable, model, prompt, cwd, prefix, writable=False):
            """Принимает аргументы Runner; возвращает ответ настроенного этапа."""
            return self.run(stage, prompt, cwd, prefix, agent=agent, writable=writable)
        return invoke
