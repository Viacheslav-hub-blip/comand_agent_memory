"""Выполнение модельных этапов с выбранными настройками и общими пожеланиями.

Функция build_prompt добавляет пожелания. StageRuntime связывает этап, модель и журналы.
Методы: __init__, validate, adapter, fingerprint, save, run, runner;
вложенная invoke возвращает исполнитель с общим контрактом Runner.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from command_agent_memory.application_config import STAGES, StageSettings, resolve_path
from command_agent_memory.model_execution.provider_registry import load_model_adapter
from command_agent_memory.model_execution.execution_policy import execution_policy
from command_agent_memory.skill_resources import skill_fingerprints


def build_prompt(prompt: str, wishes: str) -> str:
    """Принимает задание и пожелания; возвращает prompt с отдельным разделом пожеланий."""
    if not wishes.strip():
        return prompt
    return "Пожелания пользователя к работе на каждом этапе:\n" + wishes + "\n\nЗадание этапа:\n" + prompt


class StageRuntime:
    """Принимает конфигурацию и базовый каталог; хранит настройки и снимок пожеланий.

    Провайдер загружается лениво. validate проверяет все выбранные этапы до
    первого запроса; runner возвращает исполнитель этапа с контрактом Runner.
    """

    def __init__(self, configuration: dict, base: Path):
        """Принимает настройки; читает пожелания один раз, создаёт проверенные параметры."""
        defaults = configuration.get("defaults", {})
        stages = configuration.get("stages", {})
        if isinstance(stages, dict) and "replay_agent" in stages:
            raise ValueError("Перенесите stages.replay_agent в agent.factory/options сценария: "
                             "replay использует штатного рабочего агента.")
        if not isinstance(defaults, dict) or not isinstance(stages, dict) or set(stages) - STAGES:
            raise ValueError("Некорректные defaults или имена stages.")
        self.settings = {}
        self.adapters = {}
        self.skills = skill_fingerprints()
        self.wishes = ""
        if configuration.get("wishes_file") is not None:
            self.wishes = resolve_path(base, configuration["wishes_file"]).read_text(encoding="utf-8-sig")
        for name in STAGES:
            override = stages.get(name, {})
            if not isinstance(override, dict) or set(override) - {"provider", "model", "executable", "timeout_seconds", "max_model_calls"}:
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
            timeout = entry.get("timeout_seconds", 300 if name == "review" else None)
            calls = entry.get("max_model_calls", 24 if name == "review" else None)
            if entry["provider"] == "codex":
                if "max_model_calls" in entry:
                    raise ValueError("Codex CLI поддерживает timeout_seconds, но не max_model_calls.")
                calls = None
            for value in (timeout, calls):
                if value is not None and (type(value) is not int or value < 1):
                    raise ValueError("Бюджеты этапа должны быть положительными целыми числами.")
            if ("timeout_seconds" in entry and timeout is None) or ("max_model_calls" in entry and calls is None):
                raise ValueError("Заданный бюджет этапа не может быть null.")
            self.settings[name] = StageSettings(entry["provider"], entry["model"], executable, timeout, calls)

    def validate(self, stages, *, load_providers: bool = False) -> None:
        """Принимает имена этапов; проверяет настройки и при запросе доступность провайдеров."""
        from command_agent_memory.model_execution.provider_registry import registered_model_adapters

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
        """Без входов возвращает SHA-256 настроек, пожеланий и ресурсов skills.

        Изменение инструкций во время текущего запуска вызывает RuntimeError:
        результат нельзя пометить версией инструкций, с которыми он не выполнялся.
        """
        if skill_fingerprints() != self.skills:
            raise RuntimeError("Инструкции skills изменились во время запуска; повторите команду.")
        value = {"stages": {key: asdict(value) for key, value in self.settings.items()},
                 "wishes": self.wishes, "skills": self.skills}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def save(self, directory: Path) -> None:
        """Принимает каталог; сохраняет настройки, пожелания и хеши skills без секретов."""
        self.fingerprint()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "skills.json").write_text(json.dumps(
            self.skills, ensure_ascii=False, indent=2), encoding="utf-8")
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
        budgets = {key: value for key, value in {"timeout_seconds": setting.timeout_seconds,
                                                "max_model_calls": setting.max_model_calls}.items()
                   if value is not None}
        with execution_policy(**budgets):
            return runner(adapter.executable, setting.model, build_prompt(prompt, self.wishes),
                          cwd, prefix, writable=writable)

    def runner(self, stage: str, *, agent: bool = True):
        """Принимает имя этапа; возвращает Runner с его моделью и общими пожеланиями."""
        def invoke(executable, model, prompt, cwd, prefix, writable=False):
            """Принимает аргументы Runner; возвращает ответ настроенного этапа."""
            return self.run(stage, prompt, cwd, prefix, agent=agent, writable=writable)
        return invoke
