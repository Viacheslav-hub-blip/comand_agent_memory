"""Проверка автономной поставки в изоляции от исходников и модельных библиотек.

Класс StandaloneSkillsTests: setUp готовит архив и проект, io вызывает операцию,
draft создаёт материалы этапов, test_publication проверяет ревью и повтор,
test_sqlite_and_team проверяет SQLite и локальную доставку, test_git_team проверяет
Git-вклад и получение снимка, team_decision готовит ответ командного этапа,
test_pages проверяет полноту чтения длинной сессии, test_replay проверяет публикацию после сравнения.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from command_agent_memory.history_analysis.session_analysis import history_input
from scripts.build_memory_skill import build_standalone


class StandaloneSkillsTests(unittest.TestCase):
    """Принимает сценарии unittest; проверяет автономный архив на временных данных."""

    def setUp(self):
        """Без входов создаёт изолированную установку и тестовый конфиг, возвращает None."""
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        archive = build_standalone(Path(__file__).resolve().parents[1], self.root / "bundle.zip")
        with ZipFile(archive) as package:
            names = package.namelist()
            self.assertEqual(sum(name.count("/") == 1 and name.endswith("/SKILL.md") for name in names), 10)
            self.assertFalse(any("memory_pipeline.py" in n or "/model_execution/" in n for n in names))
            package.extractall(self.root / "installed")
        self.script = self.root / "installed/agent-memory-cycle/scripts/memory_io.py"
        self.path = self.root / "config.json"
        self.memory = {"root": "live", "personal": ["AGENTS.md"], "knowledge": ["knowledge"]}
        self.user = {"user_id": "alice", "workspace": "runs", "memory": self.memory,
                     "source": {"type": "jsonl", "path": "history.jsonl"}}
        self.config = {"users": [self.user]}
        (self.root / "history.jsonl").write_text('{"session_id":"s1","content":"Пример"}\n', encoding="utf-8")
        (self.root / "recipient").mkdir()

    def io(self, action, *options, ok=True):
        """Принимает операцию и флаги; проверяет код выхода и возвращает JSON автономного процесса."""
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", "-I", "-S", "-X", "utf8", str(self.script),
                                 action, "--config", str(self.path), *map(str, options)], cwd=self.root,
                                env=dict(os.environ), capture_output=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 0 if ok else 1, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def draft(self, *options):
        """Принимает флаги подготовки; возвращает каталог с раздельными тестовыми журналами этапов."""
        run = Path(self.io("prepare", *options)["run"])
        (run / "draft/AGENTS.md").write_text("Личное правило", encoding="utf-8")
        wiki = run / "draft/knowledge/wiki"
        wiki.mkdir(parents=True)
        (wiki / "metric.md").write_text("Знание команды", encoding="utf-8")
        stages = {}
        for stage in ("context-000001", "procedures-000001", "consolidation", "knowledge", "review"):
            report = "review.json" if stage == "review" else stage + ".md"
            stages[stage] = {"session_id": stage, "report": report}
            (run / report).write_text("Отчёт сессии", encoding="utf-8")
        (run / "stages.json").write_text(json.dumps(stages), encoding="utf-8")
        (run / "review.json").write_text(json.dumps({"decision": "accepted", "summary": "ok", "findings": []}))
        return run

    def test_publication(self):
        """Без входов проверяет отказ без сценария, независимое ревью, защиту снимка и повторный запуск."""
        self.io("prepare", "--replay", ok=False)
        self.assertFalse((self.root / "runs").exists())
        run = self.draft("--profile", "test-host")
        self.io("publish", "--run", run, ok=False)
        stages = json.loads((run / "stages.json").read_text())
        wrong = {**stages, "review": {**stages["review"], "session_id": "consolidation"}}
        (run / "stages.json").write_text(json.dumps(wrong))
        self.io("seal", "--run", run, ok=False)
        (run / "stages.json").write_text(json.dumps(stages))
        self.io("seal", "--run", run)
        live = self.root / "live"
        live.mkdir()
        (live / "AGENTS.md").write_text("Внешняя запись", encoding="utf-8")
        self.io("publish", "--run", run, ok=False)
        self.assertEqual((live / "AGENTS.md").read_text(encoding="utf-8"), "Внешняя запись")
        (live / "AGENTS.md").unlink()
        self.assertEqual(self.io("publish", "--run", run)["state"], "published")
        self.assertEqual(self.io("prepare", "--profile", "test-host")["state"], "reused")

    def team_decision(self, material):
        """Принимает пакет знаний; возвращает путь JSON-решения с версией основания и source-ID."""
        sources = list(material["sources"])
        self.assertEqual(len(sources), 1)
        self.assertNotIn("Личное правило", json.dumps(material, ensure_ascii=False))
        update = {"id": "metric", "status": "accepted", "statement": "Метрика", "conditions": "Команда",
                  "reason": "Проверено", "evidence_ids": sources, "conflicts_with": [],
                  "documents": {"wiki/metric.md": {"source_id": sources[0]}}}
        decision = {"input_sha256": material["input_sha256"], "result": {"decision": "accepted",
                    "summary": "ok", "findings": [], "updates": [update]}}
        path = self.root / "decision.json"
        path.write_text(json.dumps(decision), encoding="utf-8")
        return path

    def test_sqlite_and_team(self):
        """Без входов проверяет публикацию в SQLite, привязку решения к реестру и локальную доставку."""
        self.memory.pop("root")
        self.memory.update(type="sqlite", path="memory.db")
        self.config["team"] = {"root": "shared", "workspaces": ["recipient"]}
        run = self.draft()
        self.io("seal", "--run", run)
        self.io("publish", "--run", run)
        material = self.io("team-input", "--accepted", run)
        decision = self.team_decision(material)
        self.assertEqual(self.io("team-apply", "--accepted", run, "--decision", decision)["state"], "published")
        self.io("team-apply", "--accepted", run, "--decision", decision, ok=False)
        self.io("team-sync")
        self.assertEqual((self.root / "recipient/team_knowledge/wiki/metric.md").read_text(encoding="utf-8"),
                         "Знание команды")

    def test_git_team(self):
        """Без входов проводит вклад до получателя через локальный bare Git и сверяет квитанцию."""
        for args in (["init", "--bare", "--template=", "--initial-branch=main", "remote.git"],
                     ["init", "--template=", "--initial-branch=main", "seed"],
                     ["-C", "seed", "-c", "user.name=T", "-c", "user.email=t@example.test",
                      "commit", "--allow-empty", "-m", "init"], ["-C", "seed", "push", "../remote.git", "main"]):
            result = subprocess.run(["git", *args], cwd=self.root, env=dict(os.environ), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.config["team"] = {"type": "git", "team_id": "demo", "repository": "./remote.git",
                               "cache_dir": "cache", "export_paths": ["knowledge/wiki"], "workspaces": ["recipient"]}
        run = self.draft()
        self.io("seal", "--run", run)
        self.io("publish", "--run", run)
        self.io("team-submit", "--accepted", run)
        self.assertEqual(self.io("team-status")["contributions"][0]["state"], "awaiting_processing")
        identifier, = self.io("team-input")["pending"]
        flags = ("--contribution", identifier)
        decision = self.team_decision(self.io("team-input", *flags))
        receipt = self.io("team-apply", *flags, "--decision", decision)
        self.assertEqual(receipt["state"], "applied")
        self.assertEqual(self.io("team-status")["contributions"][0]["knowledge"][0]["status"], "accepted")
        self.assertEqual(self.io("team-input")["pending"], [])
        self.assertEqual(self.io("team-sync")["commit"], receipt["commit"])

    def test_replay(self):
        """Без входов проверяет публикацию файловой памяти после двух сессий и отказ при повторе ID."""
        for folder, file in (("inputs", "data.txt"), ("reference", "answer.md")):
            (self.root / folder).mkdir()
            (self.root / folder / file).write_text("ok")
        self.user["replay_scenario"] = "scenario.json"
        (self.root / "scenario.json").write_text(json.dumps({
            "messages": ["Задача"], "inputs": "inputs", "reference": "reference", "goal": "Ответ"}))
        run = self.draft("--replay")
        self.io("seal", "--run", run)
        self.io("replay-inputs", "--run", run)
        result = {"session_id": "first", "state": "completed", "turns": [
            {"message_index": 1, "user": "Задача", "answer": "Ответ"}], "profile": {
            "model": "test", "model_parameters": {}, "system_prompt_sha256": "a" * 64,
            "implementation": "fixture", "environment": {},
            "tools": [{"name": "read", "schema": {}, "implementation": "fixture"}]}}
        for side in ("baseline", "candidate"):
            (run / "replay" / side / "result.json").write_text(json.dumps(result))
        (run / "replay/decision.json").write_text(json.dumps({"choice": "candidate", "summary": "ok"}))
        self.assertIn("сессии", self.io("publish", "--run", run, ok=False)["error"])
        result["session_id"] = "second"
        (run / "replay/candidate/result.json").write_text(json.dumps(result))
        self.assertEqual(self.io("publish", "--run", run)["replay_choice"], "candidate")

    def test_pages(self):
        """Без входов проверяет все символы длинного события, порядок страниц и размер задания."""
        history = self.root / "unicode.jsonl"
        original = json.dumps({"content": "я😀" * 30_000}, ensure_ascii=False) + "\n"
        history.write_bytes(original.encode())
        prompt = history_input(history, 1)
        self.assertLess(len(prompt), 1000)
        pages = sorted(history.with_suffix(".parts").glob("*.txt"))
        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(p.read_text(encoding="utf-8") for p in pages), original)
