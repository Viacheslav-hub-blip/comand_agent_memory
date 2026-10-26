"""Чтение и проверка сценариев синтетических диалогов.

Функции: load_dialogues — загрузка упорядоченных Markdown-сценариев и привязка
их к тестовым участникам команды.
"""
from pathlib import Path
import re


PARTICIPANTS = (("101", "Анна"), ("102", "Борис"), ("103", "Вера"), ("104", "Денис"))


def load_dialogues(project_root: Path) -> list[dict]:
    """Принимает корень проекта; возвращает проверенный список тестовых диалогов.

    Из каждого Markdown извлекаются только четыре блока ``text``. Инструкции
    оператора, эталон и контрольные запросы в сообщения агента не включаются.
    """
    files = sorted((project_root / "docs/examples/synthetic-team/chats").glob("*.md"))
    participants = list(PARTICIPANTS) * 2
    if len(files) != len(participants):
        raise ValueError("Ожидались восемь файлов рабочих сценариев.")
    dialogues = []
    for number, (path, (user_id, name)) in enumerate(zip(files, participants), start=1):
        if not path.name.startswith(f"{number:02d}-"):
            raise ValueError(f"Нарушен порядок сценариев: {path.name}")
        messages = re.findall(r"```text\r?\n(.*?)\r?\n```", path.read_text(encoding="utf-8"), re.S)
        if len(messages) != 4 or any("\n" in message or not message.strip() for message in messages):
            raise ValueError(f"Нужны четыре непустые однострочные реплики: {path.name}")
        dialogues.append({
            "scenario": f"C{number:02d}", "user_id": user_id,
            "name": name, "source": path.name, "messages": messages,
        })
    return dialogues

