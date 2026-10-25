"""Создание сценария проверочного прогона из сохранённого диалога Codex.

Вход: каталог отчётов анализа с manifest.json и экспортированными историями.
Выход: scenario.json, копии входных файлов и прежнего результата.
Функции: message_text — извлечение текста сообщения; clean_request — удаление
служебной обёртки; dialogue_turns — сбор пар запроса и ответа;
mentioned_paths — поиск локальных путей; copy_evidence — копирование файлов;
build_replay_scenario — выбор диалога и создание полного сценария.
"""
from html import unescape
import json
from pathlib import Path
import re
import shutil


IGNORED_USER_PREFIXES = (
    "<recommended_plugins>", "<environment_context>", "<app-context>",
    "<skills", "# AGENTS.md instructions",
)


def message_text(payload: dict) -> str:
    """Принимает payload сообщения Codex; возвращает объединённый текст его частей."""
    parts = []
    for item in payload.get("content", []):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return unescape("\n".join(parts)).strip()


def clean_request(text: str) -> str:
    """Принимает пользовательское сообщение; возвращает запрос без списка приложений."""
    marker = "## My request"
    if marker in text:
        request = text.split(marker, 1)[1].lstrip(" :\n").strip()
        return re.sub(r"^for Codex:\s*", "", request, flags=re.IGNORECASE)
    return text.strip()


def dialogue_turns(history: Path) -> list[tuple[str, str, str]]:
    """Принимает JSONL-историю; возвращает запросы, ответы и исходные сообщения.

    Служебные сообщения среды пропускаются. Ответом считается последнее сообщение
    assistant перед следующим пользовательским запросом.
    """
    turns = []
    request = ""
    source = ""
    answers = []
    for line in history.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        payload = record.get("payload", {})
        if (record.get("type") != "response_item" or payload.get("type") != "message"
                or payload.get("role") not in ("user", "assistant")):
            continue
        text = message_text(payload)
        if not text:
            continue
        if payload["role"] == "user":
            if text.startswith(IGNORED_USER_PREFIXES):
                continue
            if request and answers:
                turns.append((request, answers[-1], source))
            request, source, answers = clean_request(text), text, []
        elif request:
            answers.append(text)
    if request and answers:
        turns.append((request, answers[-1], source))
    return turns


def mentioned_paths(text: str) -> list[Path]:
    """Принимает текст Codex; возвращает существующие абсолютные пути без повторов."""
    candidates = []
    patterns = (
        r"^##[^\n]*?:\s+([^\n]+)$",
        r"\]\(<?([A-Za-z]:[\\/][^)>]+)>?\)",
        r'path="([A-Za-z]:[\\/][^"]+)"',
    )
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text, flags=re.MULTILINE))
    result = []
    seen = set()
    for value in candidates:
        value = value.strip().removeprefix("\\\\?\\")
        path = Path(value).resolve()
        marker = str(path).casefold()
        if marker not in seen and path.exists():
            seen.add(marker)
            result.append(path)
    return result


def copy_evidence(paths: list[Path], destination: Path) -> int:
    """Принимает пути и каталог; копирует свидетельства под уникальными именами, возвращает число."""
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in paths:
        name = source.name
        target = destination / name
        if target.exists():
            target = destination / f"{copied}-{name}"
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        copied += 1
    return copied


def build_replay_scenario(reports: Path, destination: Path,
                          session_id: str | None = None) -> Path:
    """Принимает отчёты, новый каталог и необязательный ID; возвращает scenario.json.

    Выбирает завершённый Codex-диалог с существующими входными файлами и прежними
    артефактами. При явном session_id использует только указанный диалог.
    """
    candidates = []
    for manifest_path in sorted(reports.glob("session-*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest.get("state") != "completed" or manifest.get("source") != "codex":
            continue
        if session_id is not None and manifest.get("session_id") != session_id:
            continue
        history = Path(manifest["history"])
        if not history.is_file():
            continue
        for request, answer, source in dialogue_turns(history):
            inputs = mentioned_paths(source)
            references = mentioned_paths(answer)
            score = len(inputs) * 100 + len(references) * 10 + min(len(request), 1000) / 1000
            candidates.append((score, manifest, history, request, answer, inputs, references))
    if not candidates:
        suffix = f" {session_id}" if session_id else ""
        raise ValueError(f"Не найден полный диалог Codex для сценария{suffix}.")
    _, manifest, history, request, answer, inputs, references = max(candidates, key=lambda item: item[0])
    if not inputs:
        raise ValueError("В выбранном запросе не найдены доступные входные файлы.")
    if destination.exists():
        shutil.rmtree(destination)
    inputs_directory = destination / "inputs"
    reference_directory = destination / "reference"
    copy_evidence(inputs, inputs_directory)
    copy_evidence(references, reference_directory / "files")
    reference_directory.mkdir(parents=True, exist_ok=True)
    (reference_directory / "answer.md").write_text(answer, encoding="utf-8")
    scenario = {
        "request": request,
        "inputs": "inputs",
        "reference": "reference",
        "history": str(history.resolve()),
        "reference_metrics": None,
        "resource_notes": (
            "Сценарий автоматически собран из диалога Codex. Входы — файлы из запроса; "
            "эталон — финальный ответ и доступные файлы прежнего результата."
        ),
        "source_session_id": manifest["session_id"],
        "source_project": manifest["source_session"]["project"],
    }
    destination.mkdir(parents=True, exist_ok=True)
    scenario_path = destination / "scenario.json"
    scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    return scenario_path
