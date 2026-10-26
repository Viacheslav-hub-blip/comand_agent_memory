"""Формирование идентификаторов и каталогов запусков.

Функции: new_run_id — новый сортируемый идентификатор; create_run_directory —
безопасное создание уникального каталога внутри указанного раздела данных.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def new_run_id(moment: datetime | None = None) -> str:
    """Принимает необязательное время; возвращает уникальный сортируемый ID запуска."""
    current = moment or datetime.now()
    return current.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:12]


def create_run_directory(parent: Path, run_id: str | None = None) -> Path:
    """Принимает родительский каталог и ID; создаёт новый каталог и возвращает путь.

    Существующий каталог не переиспользуется: ``FileExistsError`` защищает
    артефакты предыдущих запусков от случайной перезаписи.
    """
    directory = parent.resolve() / (run_id or new_run_id())
    directory.mkdir(parents=True, exist_ok=False)
    return directory

