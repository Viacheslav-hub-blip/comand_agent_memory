"""Проверка независимости хранения и доставки от модельной обработки памяти.

Вход: исходные модули приложения. Выход: результат unittest без вызовов моделей.
Класс ModuleBoundaryTests содержит test_storage_and_delivery_keep_lower_level_dependencies:
проверку импортов, включая импорты внутри функций и методов.
"""
import ast
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "command_agent_memory"


class ModuleBoundaryTests(unittest.TestCase):
    """Без внешних ресурсов проверяет направление зависимостей групп приложения."""

    def test_storage_and_delivery_keep_lower_level_dependencies(self):
        """Без аргументов проверяет независимость хранения и доставки; возвращает None."""
        forbidden = (
            "command_agent_memory.memory_update",
            "command_agent_memory.memory_replay",
            "command_agent_memory.history_analysis",
            "command_agent_memory.model_execution",
        )
        for group in ("memory_storage", "team_memory_delivery"):
            for path in (PACKAGE_ROOT / group).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    imports = []
                    if isinstance(node, ast.ImportFrom):
                        imports = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        imports = [alias.name for alias in node.names]
                    for name in imports:
                        with self.subTest(file=path.name, module=name):
                            self.assertFalse(name.startswith(forbidden),
                                             f"{group}/{path.name} зависит от модельного этапа {name}")


if __name__ == "__main__":
    unittest.main()
