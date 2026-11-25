"""Сквозные проверки скилла настройки из распакованных архивов.

Класс AgentMemorySetupTests содержит подготовку временного проекта setUp, помощники
run_script, setup, write_session и тесты test_discovery_and_reuse,
test_explicit_history и test_missing_history_does_not_save_config.
"""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
import subprocess

from scripts.build_memory_skill import build_skill, build_standalone


REPOSITORY = Path(__file__).resolve().parents[1]


class AgentMemorySetupTests(unittest.TestCase):
    """Проверяет автономную настройку из ZIP на синтетической истории временного проекта."""

    def setUp(self):
        """Готовит проект и окружение с изолированными журналами, возвращает None."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.config = self.project / "memory_pipeline.json"
        self.candidate = self.root / "candidate.json"
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        self.environment = {**os.environ, "CODEX_HOME": str(self.codex),
                            "CLAUDE_CONFIG_DIR": str(self.claude)}
        self.skill_paths = {}
        for name, builder in (("standalone", build_standalone), ("bridge", build_skill)):
            bundle = builder(REPOSITORY, self.root / f"{name}.zip")
            target = self.root / name
            with ZipFile(bundle) as archive:
                archive.extractall(target)
            self.skill_paths[name] = target

    def run_script(self, script: Path, *arguments: str, expected_code: int = 0) -> dict:
        """Принимает скрипт и аргументы, проверяет код завершения и возвращает JSON."""
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(script), *arguments],
                                cwd=self.project, env=self.environment, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, expected_code, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def setup(self, bundle: str, action: str, *arguments: str, expected_code: int = 0) -> dict:
        """Принимает имя комплекта и команду настройки, возвращает её результат."""
        script = self.skill_paths[bundle] / "agent-memory-setup/scripts/setup_memory.py"
        return self.run_script(script, action, "--project", str(self.project), *arguments,
                               expected_code=expected_code)

    def write_session(self, host: str, identifier: str, project: Path, order: int) -> None:
        """Принимает клиент, параметры сессии и порядок, записывает один тестовый журнал."""
        if host == "codex":
            path = self.codex / "sessions" / f"rollout-{identifier}.jsonl"
            rows = [{"type": "session_meta", "payload": {"id": identifier, "cwd": str(project)}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                         "content": [{"type": "input_text", "text": "Проверь файл."}]}}]
        else:
            path = self.claude / "projects" / f"{identifier}.jsonl"
            rows = [{"type": "user", "sessionId": identifier, "cwd": str(project),
                     "message": {"role": "user", "content": "Проверь файл."}}]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        os.utime(path, (order, order))

    def test_discovery_and_reuse(self):
        """Проверяет поиск по cwd, сохранение конфига и автоматический выбор следующей сессии."""
        for bundle, host, personal in (("standalone", "codex", "AGENTS.md"), ("bridge", "claude", "CLAUDE.md")):
            with self.subTest(host=host):
                self.write_session(host, "initial", self.project, 1_700_000_000)
                self.write_session(host, "foreign", self.root / "other/project", 1_700_000_002)
                discovered = self.setup(bundle, "discover", "--host", host)
                self.assertEqual(discovered["users"][0]["sessions"][0]["id"], "initial")
                settings = discovered["configuration"]
                self.assertEqual(settings["users"][0]["memory"]["personal"], [personal])
                self.assertNotIn("session_ids", settings["users"][0]["source"])
                self.candidate.write_text(json.dumps(settings), encoding="utf-8")
                self.assertEqual(self.setup(bundle, "save", "--config", str(self.candidate))["state"], "saved")
                saved = self.config.read_bytes()
                self.assertEqual(self.setup(bundle, "save", "--config", str(self.candidate),
                                            expected_code=1)["state"], "failed")
                self.assertEqual(self.config.read_bytes(), saved)
                self.write_session(host, "next", self.project, 1_700_000_001)
                checked = self.setup(bundle, "check")
                self.assertEqual(checked["users"][0]["sessions"][0]["id"], "next")
                if bundle == "standalone":
                    script = self.skill_paths[bundle] / "agent-memory-cycle/scripts/memory_io.py"
                    status = self.run_script(script, "check", "--config", str(self.config))
                    self.assertEqual(status["sessions"], {"local": 1})
                else:
                    script = self.skill_paths[bundle] / "update-agent-memory/scripts/run_memory.py"
                    status = self.run_script(script, "run", "--host", host, "--project", str(self.project),
                                             "--validate-only")
                    self.assertEqual(status["users"][0]["sessions"], 1)
                self.assertFalse((self.project / personal).exists())
                self.assertFalse((self.project / ".agent-memory").exists())
                self.config.unlink()

    def test_explicit_history(self):
        """Проверяет явный JSONL GigaCode и экспорт checkpoints GigaAgent."""
        (self.project / "giga-code.jsonl").write_text(
            '{"session_id":"code-1","role":"user","content":"Проверь."}\n', encoding="utf-8")
        code = self.setup("bridge", "discover", "--host", "gigacode", "--history", "giga-code.jsonl")
        self.assertEqual(code["configuration"]["users"][0]["memory"]["personal"], ["GIGACODE.md"])
        self.assertEqual(code["users"][0]["sessions"], [{"id": "code-1", "events": 1}])
        history = [{"checkpoint": {"checkpoint_id": "checkpoint"}, "parent_checkpoint": None,
                    "values": {"messages": [{"id": "message", "type": "human", "content": "Проверь."}]}}]
        (self.project / "giga-agent.json").write_text(
            json.dumps({"threads": [{"thread_id": "agent-1", "history": history}]}), encoding="utf-8")
        agent = self.setup("standalone", "discover", "--host", "gigaagent", "--history", "giga-agent.json")
        self.assertEqual(agent["users"][0]["sessions"], [{"id": "agent-1", "events": 1}])

    def test_missing_history_does_not_save_config(self):
        """Проверяет отказ для неизвестного пути до создания рабочего файла настроек."""
        self.assertEqual(self.setup("standalone", "discover", "--host", "codex",
                                    expected_code=1)["state"], "failed")
        settings = {"users": [{"user_id": "local", "workspace": ".agent-memory/runs",
                               "source": {"type": "jsonl", "path": "missing.jsonl"},
                               "memory": {"root": ".", "personal": ["AGENTS.md"]}}]}
        self.candidate.write_text(json.dumps(settings), encoding="utf-8")
        self.assertEqual(self.setup("standalone", "save", "--config", str(self.candidate),
                                    expected_code=1)["state"], "failed")
        self.assertFalse(self.config.exists())
