"""Последовательный запуск восьми синтетических чатов команды через DeepAgents.

Вход: реплики из docs/examples/synthetic-team/chats, материалы synthetic_team
и модель с ключом из settings.py. Аргумент --check проверяет входы без вызова модели.
Выход: сообщения в терминале, отдельная память и SQLite каждого сотрудника,
журнал сеансов в data/synthetic-team-runs/<идентификатор запуска>/sessions.json.

Функции: run_dialogue — выполнить один диалог с отдельным контекстом и показать
сообщения; main — подготовить рабочие области и последовательно выполнить
сценарии. Загрузку сценариев предоставляет command_agent_memory.synthetic.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from uuid import uuid4

from agent_runtime import create_agent, open_chat_database, run_turn
from command_agent_memory.synthetic import load_dialogues


def run_dialogue(workspace: Path, dialogue: dict, connection: sqlite3.Connection, session_id: str) -> None:
    """Выполняет сценарий; вход — область, реплики, соединение и ID; выход — записи и печать.

    Создаёт отдельного агента через agent_runtime. Ошибка прерывает прогон;
    уже полученные сообщения сохраняются в базе. Возвращает None.
    """
    from langchain_core.messages import HumanMessage

    agent = create_agent(workspace)
    for number, text in enumerate(dialogue["messages"], start=1):
        print(f"\n{'=' * 72}\n{dialogue['scenario']} · {dialogue['name']} · реплика {number}/4", flush=True)
        HumanMessage(content=text).pretty_print()
        for message in run_turn(agent, text, connection, session_id, dialogue["user_id"]):
            message.pretty_print()
        sys.stdout.flush()


def main() -> None:
    """Подготовить и выполнить прогон; вход — аргументы командной строки и файлы.

    Выход — восемь чатов в отдельных пользовательских областях и sessions.json.
    При --check проверяются только сценарии и наличие материалов; модель не
    создаётся и папка запуска не записывается. Функция ничего не возвращает.
    """
    parser = argparse.ArgumentParser(description="Автоматический прогон диалогов команды «Север».")
    parser.add_argument("--check", action="store_true", help="Проверить входы без модели и записи логов.")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent
    dialogues = load_dialogues(project_root)
    materials = project_root / "user_workspace/synthetic_team"
    for dialogue in dialogues:
        for text in dialogue["messages"]:
            for location in re.findall(r"/synthetic_team/([\w/.-]+\.(?:csv|md|sql))", text):
                if not (materials / location).is_file():
                    raise FileNotFoundError(materials / location)
    if args.check:
        print("Проверка пройдена: 8 диалогов, 32 реплики, все указанные входные файлы существуют.")
        return

    import settings

    if not settings.openrouter_api_key:
        raise ValueError("Заполните openrouter_api_key в settings.py.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_directory = project_root / "data/synthetic-team-runs" / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    sessions = []
    print(f"Модель: {settings.model}\nРезультаты: {run_directory}\nОстановка: Ctrl+C", flush=True)
    for dialogue in dialogues:
        workspace = run_directory / "users" / dialogue["user_id"] / "user_workspace"
        if not workspace.exists():
            workspace.mkdir(parents=True)
            shutil.copytree(materials, workspace / "synthetic_team", ignore=shutil.ignore_patterns("outputs", "__pycache__"))
            (workspace / "skills").mkdir()
            (workspace / "USER_PREFERENCES.md").write_text("# Предпочтения пользователя\n", encoding="utf-8")
            (workspace / "AGENTS.md").write_text(
                "# Техническое окружение тестового прогона\n\n"
                "- Оболочка execute — Windows cmd.exe.\n"
                f'- Для Python используй интерпретатор "{sys.executable}".\n'
                f"- Физический корень рабочей области: {workspace.as_posix()}.\n"
                "- В execute используй физические пути, в файловых инструментах — виртуальные.\n",
                encoding="utf-8",
            )
        session_id = str(uuid4())
        entry = {"scenario": dialogue["scenario"], "user_id": dialogue["user_id"],
                 "name": dialogue["name"], "source": dialogue["source"], "session_id": session_id,
                 "workspace": str(workspace), "database": str(workspace / "chat_logs.db"),
                 "status": "running"}
        sessions.append(entry)
        manifest = run_directory / "sessions.json"
        manifest.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
        connection = open_chat_database(workspace / "chat_logs.db")
        try:
            print(f"\n{dialogue['scenario']} · {dialogue['name']} · сеанс {session_id}", flush=True)
            run_dialogue(workspace, dialogue, connection, session_id)
            entry["status"] = "completed"
        except KeyboardInterrupt:
            entry["status"] = "interrupted"
            print("\nПрогон остановлен. Полученные сообщения сохранены.", flush=True)
            return
        except Exception:
            entry["status"] = "failed"
            raise
        finally:
            connection.close()
            manifest.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово: выполнены все 8 диалогов. Журнал: {run_directory / 'sessions.json'}", flush=True)


if __name__ == "__main__":
    main()
