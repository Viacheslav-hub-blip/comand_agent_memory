"""Проверка новых точек расширения проекта без внешних модельных вызовов.

Класс PackageArchitectureTests проверяет ID запусков, защиту каталогов,
загрузку synthetic-сценариев и фабрику адаптеров.
"""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.adapters import load_model_adapter
from command_agent_memory.runs import create_run_directory, new_run_id
from command_agent_memory.synthetic import load_dialogues


class PackageArchitectureTests(unittest.TestCase):
    """Проверяет публичные контракты новых пакетов на локальных данных."""

    def test_run_layout_is_unique_and_non_destructive(self):
        """Проверяет формат ID и запрет повторного использования каталога."""
        identifier = new_run_id(datetime(2026, 10, 25, 12, 30, 45, 123456))
        self.assertTrue(identifier.startswith("20261025T123045123456-"))
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            created = create_run_directory(parent, "fixed")
            self.assertTrue(created.is_dir())
            with self.assertRaises(FileExistsError):
                create_run_directory(parent, "fixed")

    def test_synthetic_scenarios_are_loaded(self):
        """Проверяет восемь сценариев и четыре реплики в каждом."""
        root = Path(__file__).resolve().parents[1]
        dialogues = load_dialogues(root)
        self.assertEqual(len(dialogues), 8)
        self.assertTrue(all(len(item["messages"]) == 4 for item in dialogues))

    def test_codex_adapter_uses_common_contract(self):
        """Проверяет сборку Codex-адаптера без реального запуска CLI."""
        def runner(*_args, **_kwargs):
            """Принимает тестовые аргументы и возвращает фиктивный ответ."""
            return "ok"

        with patch("codex_client.find_codex", return_value="codex"), patch(
                "codex_client.run_codex", side_effect=runner) as mocked:
            adapter = load_model_adapter("codex")
        self.assertEqual(adapter.name, "codex")
        self.assertEqual(adapter.executable, "codex")
        self.assertIs(adapter.agent_runner, mocked)
        self.assertIs(adapter.chat_runner, mocked)

    def test_unknown_adapter_is_rejected(self):
        """Проверяет понятную ошибку для незарегистрированного backend."""
        with self.assertRaisesRegex(ValueError, "Неизвестный backend"):
            load_model_adapter("missing")


if __name__ == "__main__":
    unittest.main()
