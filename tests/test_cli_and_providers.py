"""Проверка новых точек расширения проекта без внешних модельных вызовов.

Класс PackageArchitectureTests проверяет ID запусков, защиту каталогов,
конфигурационный CLI, фабрику адаптеров и границы загрузки ресурсов скилов.
"""
from datetime import datetime
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from command_agent_memory.model_providers import load_model_adapter
from command_agent_memory.run_reports import create_run_directory, new_run_id
from command_agent_memory.__main__ import main
from command_agent_memory.project_paths import get_skill_path


class PackageArchitectureTests(unittest.TestCase):
    """Проверяет публичные контракты новых пакетов на локальных данных."""

    def test_cli_requires_configuration_and_passes_supported_flags(self):
        """Без входов проверяет финальный CLI и отклонение удалённых параметров запуска."""
        with patch("command_agent_memory.__main__.run_pipeline") as run:
            main(["--config", "memory_pipeline.json", "--validate-only", "--evaluate-by-replay"])
            run.assert_called_once_with(Path("memory_pipeline.json"), validate_only=True, evaluate_by_replay=True)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--user-id", "alice"])
        self.assertEqual(error.exception.code, 2)

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


    def test_codex_adapter_uses_common_contract(self):
        """Проверяет сборку Codex-адаптера без реального запуска CLI."""
        def runner(*_args, **_kwargs):
            """Принимает тестовые аргументы и возвращает фиктивный ответ."""
            return "ok"

        with patch("command_agent_memory.model_providers.codex_cli.find_codex", return_value="codex"), patch(
                "command_agent_memory.model_providers.codex_cli.run_codex", side_effect=runner) as mocked:
            adapter = load_model_adapter("codex")
        self.assertEqual(adapter.name, "codex")
        self.assertEqual(adapter.executable, "codex")
        self.assertIs(adapter.agent_runner, mocked)
        self.assertIs(adapter.chat_runner, mocked)

    def test_unknown_adapter_is_rejected(self):
        """Проверяет понятную ошибку для незарегистрированного backend."""
        with self.assertRaisesRegex(ValueError, "Неизвестный backend"):
            load_model_adapter("missing")

    def test_skill_resources_stay_inside_selected_skill(self):
        """Принимает опасные имена и пути; проверяет отказ доступа за пределы скила."""
        for name in ("../memory", "memory_review", "memory--review", "-memory-review"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                get_skill_path(name)
        for resource in ("../knowledge-consolidation/SKILL.md", str(Path(__file__).resolve())):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                get_skill_path("memory-review", resource)

    def test_skill_loader_reports_missing_resource(self):
        """Передаёт отсутствующий ресурс; проверяет FileNotFoundError без создания файлов."""
        with self.assertRaises(FileNotFoundError):
            get_skill_path("memory-review", "references/missing.md")


if __name__ == "__main__":
    unittest.main()
