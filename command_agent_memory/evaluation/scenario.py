"""Чтение исходных пользовательских реплик для диалоговой проверки.

Функция load_user_messages читает список messages либо блоки text из файла
dialogue_source и возвращает отдельные реплики в исходном порядке.
"""

from pathlib import Path
import re


def load_user_messages(data: dict, scenario_path: Path, project_root: Path) -> tuple[str, ...]:
    """Принимает JSON сценария, его путь и корень проекта; возвращает реплики.

    Форматы messages и dialogue_source задаются по отдельности. Пустой кортеж
    сохраняет совместимость со сценариями прежнего одношагового запуска.
    """
    if "messages" in data and "dialogue_source" in data:
        raise ValueError("Выберите messages либо dialogue_source.")
    messages = data.get("messages", [])
    if "dialogue_source" in data:
        location = data["dialogue_source"]
        if not isinstance(location, str) or not location.strip():
            raise ValueError("dialogue_source должен содержать путь к исходному чату.")
        source = (scenario_path.parent / location).resolve()
        if not source.is_relative_to(project_root.resolve()) or not source.is_file():
            raise ValueError("Исходный чат должен находиться внутри проекта.")
        messages = re.findall(r"```text\r?\n(.*?)\r?\n```", source.read_text(encoding="utf-8-sig"), re.S)
    if not isinstance(messages, list) or any(not isinstance(item, str) or not item.strip() for item in messages):
        raise ValueError("messages должен содержать список отдельных непустых реплик.")
    if ("messages" in data or "dialogue_source" in data) and not messages:
        raise ValueError("Для диалога нужна хотя бы одна исходная реплика.")
    return tuple(messages)
