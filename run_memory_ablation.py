"""Воспроизводимый запуск replay с частью памяти для A/B/C/D-сравнения.

Функции: build_prompt — формирование одинаковой инструкции задачи с выбранной
памятью; copy_memory_scope — копирование разрешённой части памяти;
run_ablation — подготовка чистого окружения и запуск агента; main — CLI-вход.
"""
import argparse
import json
from pathlib import Path
import shutil

from memory_pipeline import ROOT
from openrouter_client import run_openrouter


PERSONAL_ENTRIES = ("AGENTS.md", "USER_PREFERENCES.md", "user_llm_wiki")
KNOWLEDGE_ENTRIES = ("knowledge",)


def build_prompt(workspace: Path, request: str, scope: str) -> str:
    """Принимает область, текст задачи и scope памяти; возвращает инструкцию агента.

    Варианты ``personal`` и ``knowledge`` явно перечисляют только доступные
    материалы. Это исключает поиск отсутствующих файлов из других областей.
    """
    memory_instruction = {
        "personal": (
            "Перед задачей прочитай только AGENTS.md, USER_PREFERENCES.md и "
            "user_llm_wiki/INDEX.md; они доступны лишь для чтения. "
        ),
        "knowledge": (
            "Перед задачей прочитай только knowledge/INDEX.md и применимые файлы "
            "из knowledge/wiki/, knowledge/cases/ и knowledge/skills/; они доступны "
            "лишь для чтения. "
        ),
    }[scope]
    return (
        f"Рабочая область: {workspace.as_posix()}. Реши один запрос пользователя. "
        "Используй только файлы этой рабочей области; сеть и внешние сервисы не используй. "
        + memory_instruction
        + "Результаты задачи сохраняй внутри рабочей области. Если в запросе "
        "указаны пути вида /documents/ или /synthetic_team/, разрешай их относительно "
        "физического корня этой рабочей области. Исходные данные не изменяй. "
        "В итоговом ответе сообщи полученный результат и созданные файлы.\n\n"
        "Запрос пользователя:\n" + request
    )


def copy_memory_scope(source: Path, workspace: Path, scope: str) -> None:
    """Принимает снимок памяти, рабочую область и scope; копирует разрешённые файлы.

    ``personal`` переносит личные правила и wiki, ``knowledge`` — только общую
    тематическую память. Исходный снимок и другие области не изменяются.
    """
    names = PERSONAL_ENTRIES if scope == "personal" else KNOWLEDGE_ENTRIES
    for name in names:
        origin = source / name
        destination = workspace / name
        if origin.is_dir():
            shutil.copytree(origin, destination)
        else:
            shutil.copy2(origin, destination)


def run_ablation(source: Path, inputs: Path, request: str, destination: Path,
                 model: str, scope: str) -> None:
    """Создаёт чистый replay, выполняет DeepSeek и сохраняет ответ с измерениями.

    ``source`` содержит опубликованный снимок памяти, ``inputs`` — исходную
    задачу без результатов. ``destination`` должен отсутствовать до запуска.
    """
    if destination.exists():
        raise ValueError(f"Каталог результата уже существует: {destination}")
    destination.mkdir(parents=True)
    workspace = destination / "workspace"
    workspace.mkdir()
    shutil.copytree(inputs, workspace / inputs.name)
    copy_memory_scope(source, workspace, scope)
    request_path = destination / "request.md"
    request_path.write_text(request, encoding="utf-8")
    result = destination / "result"
    result.mkdir()
    answer = run_openrouter(
        "", model, build_prompt(workspace, request, scope), workspace,
        result / "agent", writable=True,
    )
    (result / "answer.md").write_text(answer, encoding="utf-8")


def main() -> None:
    """Читает CLI-аргументы, загружает сценарий и запускает один ablation-вариант."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-source", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--scope", choices=("personal", "knowledge"), required=True)
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8-sig"))
    run_ablation(
        args.memory_source.resolve(), args.inputs.resolve(), scenario["request"],
        args.destination.resolve(), args.model, args.scope,
    )


if __name__ == "__main__":
    main()
