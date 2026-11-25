"""Проверка штатного парного replay через CLI без сторонних библиотек.

HostReplayTests: test_paired_sessions проверяет два профиля, продолжение и публикацию;
test_scenario_selection проверяет приоритет источников сценария;
fixture готовит входы; run_jobs обслуживает задания; answer возвращает ответы.
"""
import json
from pathlib import Path
import subprocess
import tempfile
from time import monotonic, sleep
import unittest

from test_native_memory_skill import PYTHON, SKILL


class HostReplayTests(unittest.TestCase):
    """Моделирует протокол хоста, сравнение и отклонение несовместимых сессий."""

    def fixture(self, project: Path) -> None:
        """Принимает каталог; создаёт синтетические диалог, вход и эталон."""
        (project / "inputs").mkdir(parents=True)
        (project / "reference").mkdir()
        (project / "inputs/numbers.txt").write_text("4,3", encoding="utf-8")
        (project / "reference/answer.md").write_text("1", encoding="utf-8")
        (project / "history.jsonl").write_text(
            '{"session_id":"demo","role":"user","content":"Правило: вычти второе число из первого."}\n',
            encoding="utf-8")
        (project / "replay_scenario.json").write_text(json.dumps({
            "messages": ["Посчитай значение.", "Вычти второе число из первого."],
            "goal": "Получено число 1.", "inputs": "inputs", "reference": "reference",
        }, ensure_ascii=False), encoding="utf-8")

    def answer(self, payload: dict, starts: list[str], turns: list[tuple[str, str]], case: str) -> str:
        """Принимает задание и накопленный контекст; возвращает синтетический ответ хоста."""
        stage = payload["stage"]
        if stage == "consolidation":
            Path(payload["write_paths"][0]).write_text("Вычти второе число из первого.", encoding="utf-8")
        elif stage == "review":
            return json.dumps({"decision": "accepted", "summary": "Проверено", "findings": []})
        elif stage == "replay_start":
            session = "s1" if not starts or case == "same_session" else "s2"
            model = "changed" if starts and case == "different_profile" else "test-model"
            starts.append(session)
            return json.dumps({"session_id": session, "profile": {
                "model": model, "model_parameters": {"effort": "low"}, "environment": {"test": True},
                "system_prompt_sha256": payload["instructions_sha256"], "implementation": "test:1",
                "tools": [{"name": "files", "implementation": "test:1", "schema": {}}],
            }})
        elif stage == "replay_turn":
            session = payload["session_id"]
            text = Path(payload["prompt"]).read_text(encoding="utf-8")
            turns.append((session, text))
            workspace = Path(payload["workspace"])
            known = (workspace / "AGENTS.md").exists() or len([value for value in turns if value[0] == session]) == 2
            if case == "input_changes":
                (workspace / "inputs/numbers.txt").write_text("0,0", encoding="utf-8")
            if known:
                (workspace / "result.md").write_text("1", encoding="utf-8")
            return json.dumps({"session_id": session, "answer": "1" if known else "Как посчитать?"})
        elif stage == "replay_tester":
            complete = turns[-1][0] == "s2" or len(turns) >= 2
            return json.dumps({"action": "complete" if complete else "send", "message_index": None if complete else 2,
                               "goal_reached": complete, "summary": "Проверено", "evidence": ["Ответ прочитан"]})
        elif stage == "evaluation":
            return json.dumps({"choice": "candidate", "summary": "Кандидат решил задачу за один ход"})
        return "# Отчёт\nПравило подтверждено."

    def run_jobs(self, project: Path, tasks: Path, case: str) -> tuple[dict, list[str], list[tuple[str, str]]]:
        """Принимает проект, каталог заданий и вариант ответа; возвращает итог и использованные сессии."""
        command = [*PYTHON, str(SKILL), "run", "--host", "codex", "--source", "jsonl", "--history",
                   "history.jsonl", "--tasks", str(tasks), "--evaluate-by-replay"]
        starts, turns = [], []
        with subprocess.Popen(command, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8") as process:
            try:
                deadline = monotonic() + 35
                while process.poll() is None and monotonic() < deadline:
                    for task in tasks.glob("*/task.json"):
                        if task.with_name("reply.json").exists() or task.with_name("finished").exists():
                            continue
                        self.assertFalse((project / "AGENTS.md").exists())
                        reply = task.with_name("answer.md")
                        reply.write_text(self.answer(json.loads(task.read_bytes()), starts, turns, case), encoding="utf-8")
                        subprocess.run([*PYTHON, str(SKILL), "reply", str(task), "--answer", str(reply)],
                                       cwd=project, check=True, capture_output=True)
                    sleep(0.05)
                stdout, stderr = process.communicate(timeout=1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertTrue(stdout, stderr)
        return json.loads(stdout), starts, turns

    def test_paired_sessions(self):
        """Без входов проверяет успешную пару, расходящиеся профили, повтор ID и изменение входов."""
        for case in ("valid", "different_profile", "same_session", "input_changes"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                project = root / "project"
                self.fixture(project)
                tasks = root / "tasks"
                result, starts, turns = self.run_jobs(project, tasks, case)
                self.assertEqual(len(starts), 2)
                self.assertEqual(len(list(tasks.glob("close-*.json"))), 2)
                if case == "valid":
                    self.assertEqual(result["state"], "completed", result)
                    self.assertEqual(result["users"][0]["replay_choice"], "candidate")
                    self.assertEqual(turns, [("s1", "Посчитай значение."), ("s1", "Вычти второе число из первого."),
                                             ("s2", "Посчитай значение.")])
                    self.assertTrue((project / "AGENTS.md").is_file())
                else:
                    self.assertEqual(result["state"], "failed", result)
                    self.assertEqual(len(turns), int(case == "input_changes"))
                    self.assertFalse((project / "AGENTS.md").exists())

    def test_scenario_selection(self):
        """Без входов проверяет сценарий по умолчанию с конфигом и явное переопределение."""
        with tempfile.TemporaryDirectory() as name:
            project = Path(name) / "project"
            self.fixture(project)
            configuration = project / "memory_pipeline.json"
            user = {"user_id": "demo", "workspace": "runs", "source": {"type": "jsonl", "path": "history.jsonl"},
                    "memory": {"type": "files", "root": ".", "personal": ["AGENTS.md"]}}
            command = [*PYTHON, str(SKILL), "run", "--host", "codex", "--evaluate-by-replay",
                       "--validate-only", "--config", configuration.name]
            settings = {"cwd": project, "capture_output": True, "encoding": "utf-8", "timeout": 10}
            configuration.write_text(json.dumps({"users": [user]}), encoding="utf-8")
            fallback = subprocess.run(command, check=True, **settings)
            self.assertEqual(json.loads(fallback.stdout)["state"], "validated")

            user["replay_scenario"] = "missing.json"
            configuration.write_text(json.dumps({"users": [user]}), encoding="utf-8")
            configured = subprocess.run(command, **settings)
            self.assertEqual(configured.returncode, 1)
            self.assertIn("Сценарий должен быть JSON-файлом", json.loads(configured.stdout)["error"])

            selected = subprocess.run([*command, "--scenario", "replay_scenario.json"], check=True, **settings)
            self.assertEqual(json.loads(selected.stdout)["state"], "validated")
