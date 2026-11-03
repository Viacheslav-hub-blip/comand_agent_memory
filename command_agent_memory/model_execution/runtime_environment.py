"""Описание фактической среды исполнения для модельных заданий.

Функция describe_execution_environment принимает рабочий каталог и режим
инструментов; возвращает сведения об ОС, Python, путях и доступной оболочке.
"""
import os
from pathlib import PurePath
import platform


def describe_execution_environment(workspace: PurePath, *, file_paths: str = "native",
                                   shell: bool = False) -> str:
    """Принимает каталог, режим путей и доступность shell; возвращает инструкцию о среде.

    ОС определяется при каждом запуске, а не берётся из истории пользователя.
    Режим native использует пути ОС, scoped — файловые инструменты с ограниченной
    записью, virtual — корень / внутри переданной рабочей области.
    """
    if file_paths not in {"native", "scoped", "virtual"}:
        raise ValueError("Неизвестный режим файловых путей.")
    system = platform.system()
    lines = ["Среда текущего исполнения (не сведения из анализируемой истории):",
             f"Операционная система: {system}. Python: {platform.python_version()}.",
             f"Рабочий каталог на машине: {workspace.as_posix()}."]
    if file_paths == "virtual":
        lines.append("Файловые инструменты используют виртуальные пути /...; / соответствует рабочему каталогу.")
    elif file_paths == "scoped" and system == "Windows":
        lines.append("Файловым инструментам передавай C:/... как /drives/C/...; другие диски аналогично. "
                     "Это представление путей инструментов, а не каталог ОС. Не ищи диск через ls.")
    else:
        lines.append("Файловые инструменты используют обычные пути текущей ОС.")
    if shell:
        command_shell = os.environ.get("COMSPEC", "cmd.exe") if system == "Windows" else "/bin/sh"
        lines.append(f"Команды execute выполняются через {command_shell}; используй её синтаксис и пути ОС.")
    else:
        lines.append("Описание среды само по себе не предоставляет shell: используй только выданные инструменты.")
    lines.append("Не переписывай пути внутри исходных документов ради представления инструментов. "
                 "Не переноси свойства этой машины в память как предпочтения пользователя.")
    return "\n".join(lines)
