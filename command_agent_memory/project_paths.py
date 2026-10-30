"""Пути к данным приложения и встроенным инструкциям.

Функция get_skill_path принимает имя скила и ресурс; возвращает путь к файлу.
ROOT указывает на корень checkout либо текущий каталог установленного пакета.
SKILLS_ROOT указывает на каталог встроенных скилов внутри пакета.
"""
from pathlib import Path


_PACKAGE = Path(__file__).resolve().parent
_REPOSITORY = _PACKAGE.parent
_IN_CHECKOUT = (_REPOSITORY / "pyproject.toml").is_file()
ROOT = _REPOSITORY if _IN_CHECKOUT else Path.cwd()
SKILLS_ROOT = _PACKAGE / "skills"


def get_skill_path(name: str, resource: str = "SKILL.md") -> Path:
    """Принимает имя скила и относительный ресурс; возвращает путь существующего файла.

    Без resource выбирает SKILL.md. Отсутствующий файл вызывает FileNotFoundError.
    Некорректное имя или выход за каталог выбранного скила вызывает ValueError.
    Функция не создаёт файлов.
    """
    if (not name or len(name) > 64 or name.startswith("-") or name.endswith("-")
            or "--" in name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in name)):
        raise ValueError("Имя скила должно содержать строчные латинские буквы, цифры и одиночные дефисы.")
    directory = SKILLS_ROOT / name
    path = directory / resource
    if (not directory.resolve().is_relative_to(SKILLS_ROOT.resolve())
            or Path(resource).is_absolute() or not path.resolve().is_relative_to(directory.resolve())):
        raise ValueError("Ресурс выходит за каталог выбранного скила.")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
