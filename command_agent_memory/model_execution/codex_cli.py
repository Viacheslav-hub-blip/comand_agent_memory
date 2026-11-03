"""Поиск и запуск Codex CLI для анализа, консолидации и ревью.

Вход: путь CLI, модель, задание, рабочая область и префикс журналов.
Выход: последний ответ; файлы prompt, JSONL, stderr, ответа и измерений.
Функции: find_codex — поиск CLI; run_codex — запуск и сохранение измерений.
"""
import json
import os
from pathlib import Path
import shutil
from time import perf_counter
import tomllib
from command_agent_memory.model_execution.execution_policy import current_execution_policy, run_bounded_process
from command_agent_memory.model_execution.runtime_environment import describe_execution_environment


def find_codex(path: Path | None = None) -> str:
    """Принимает необязательный путь CLI; возвращает путь найденного файла.

    Без пути проверяет PATH, затем установки приложения Windows. Если файл
    не найден, вызывает FileNotFoundError. Модель и авторизацию не проверяет.
    """
    if path is not None:
        executable = str(path.expanduser().resolve())
    else:
        executable = shutil.which("codex")
        if executable is None:
            local_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            directory = local_data / "OpenAI/Codex/bin"
            candidates = sorted(
                directory.glob("*/codex.exe"),
                key=lambda candidate: candidate.stat().st_mtime_ns, reverse=True,
            ) + [directory / "codex.exe"]
            executable = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    if executable is None or not Path(executable).is_file():
        raise FileNotFoundError("Codex CLI не найден. Укажи путь через --codex-path.")
    return executable


def run_codex(executable: str, model: str, prompt: str, cwd: Path,
              prefix: Path, writable: bool = False) -> str:
    """Принимает CLI, модель, задание, область, префикс и право записи; возвращает ответ.

    Сохраняет prompt, JSONL, stderr, последний ответ и metrics.json с временем
    процесса и usage завершённых turns. Отсутствующий usage записывается как null.
    При сбое сохраняет доступные измерения и передаёт исключение вызывающему коду.
    Общий пользовательский config отключён; сохраняется выбранный Windows sandbox.
    """
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prompt = describe_execution_environment(cwd.resolve()) + "\n\n" + prompt
    prefix.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
    output = prefix.with_suffix(".last.txt")
    command = [
        executable, "--ask-for-approval", "never", "exec", "--ignore-user-config",
        "--model", model, "--cd", str(cwd), "--sandbox",
        "workspace-write" if writable else "read-only",
        "--config", "sandbox_workspace_write.writable_roots=[]",
        "--skip-git-repo-check", "--ephemeral", "--json", "-o", str(output), "-",
    ]
    if os.name == "nt":
        config_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        implementation = config.get("windows", {}).get("sandbox")
        if implementation in ("elevated", "unelevated"):
            command[command.index("--model"):command.index("--model")] = [
                "--config", f'windows.sandbox="{implementation}"',
            ]
        if config.get("features", {}).get("prefer_mxc") is True:
            command[command.index("--model"):command.index("--model")] = [
                "--config", "features.prefer_mxc=true",
            ]
    print(f"Codex: {prefix.name}; модель: {model}; журнал: {prefix.with_suffix('.jsonl')}", flush=True)
    result = None
    types, usages = [], []
    started = perf_counter()
    try:
        with prefix.with_suffix(".jsonl").open("w", encoding="utf-8") as events:
            with prefix.with_suffix(".stderr.log").open("w", encoding="utf-8") as errors:
                result = run_bounded_process(
                    command, prompt, current_execution_policy().timeout_seconds or 900,
                    cwd=cwd, stdout=events, stderr=errors,
                )
    finally:
        duration = perf_counter() - started
        events_path = prefix.with_suffix(".jsonl")
        lines = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                types.append(event.get("type"))
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usages.append(event["usage"])
        usage = None
        if usages:
            usage = {}
            for turn_usage in usages:
                for key, value in turn_usage.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
        completed = (
            result is not None and result.returncode == 0 and "turn.completed" in types
            and "turn.failed" not in types and "error" not in types and output.is_file()
        )
        metrics = {
            "model": model, "duration_seconds": duration,
            "return_code": result.returncode if result is not None else None,
            "completed": completed, "usage": usage,
            "timeout_seconds": current_execution_policy().timeout_seconds or 900,
        }
        prefix.with_suffix(".metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if not completed:
        raise RuntimeError(f"Codex не завершил обработку: {prefix}")
    return output.read_text(encoding="utf-8-sig")
