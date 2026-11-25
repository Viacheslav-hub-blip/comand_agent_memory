"""Проверка переносимого скилла с локальным исполнителем заданий.

NativeSkillTests: test_packaged_skill_updates_project_without_configuration проверяет ZIP;
test_build_uses_current_sources проверяет повторную сборку из разных копий;
test_auto_history_uses_project_path проверяет выбор и ошибки истории;
test_reply_accepts_only_one_on_time проверяет гонку ответов.
Вложенные функции make_rollout и reply_to_task готовят данные и отвечают на задания.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import monotonic, sleep, time
import unittest
from zipfile import ZipFile


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL = REPOSITORY / ".agents/skills/update-agent-memory/scripts/run_memory.py"
PYTHON = [sys.executable, "-B", "-X", "utf8", "-I", "-S"]


class NativeSkillTests(unittest.TestCase):
    """Проверяет запуск без установленных провайдеров и конфигурации."""

    def test_packaged_skill_updates_project_without_configuration(self):
        """Без входов проверяет один полный цикл из ZIP с передачей реальных файлов заданий."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = root / "skill.zip"
            subprocess.run([*PYTHON, str(REPOSITORY / "scripts/build_memory_skill.py"),
                            "--output", str(archive)], cwd=root, check=True, capture_output=True)
            with ZipFile(archive) as bundle:
                bundle.extractall(root / "skills")
            script = root / "skills/update-agent-memory/scripts/run_memory.py"
            project = root / "project"
            project.mkdir()
            history = project / "history.jsonl"
            history.write_text('{"session_id":"test","role":"user","content":"Сохрани образец."}\n',
                               encoding="utf-8")
            tasks = root / "tasks"
            memory = project / "AGENTS.md"
            ignores = project / ".agent-memory/runs/.gitignore"
            calls = []

            def reply_to_task(task: Path) -> None:
                """Принимает задание; пишет синтетический ответ нужного этапа, возвращает None."""
                payload = json.loads(task.read_text(encoding="utf-8"))
                stage = payload["stage"]
                calls.append(stage)
                self.assertTrue(Path(payload["prompt"]).is_file())
                self.assertFalse(memory.exists())
                answer = "# Отчёт\nОписано проверочное правило."
                if stage in {"context", "procedures"}:
                    self.assertEqual(payload["write_paths"], [])
                if stage == "consolidation":
                    target, = map(Path, payload["write_paths"])
                    self.assertEqual(target.name, "AGENTS.md")
                    target.write_text("# Проверенное правило\n", encoding="utf-8")
                if stage == "review":
                    answer = json.dumps({"decision": "accepted", "summary": "Проверено", "findings": []})
                output = task.with_name("answer.md")
                output.write_text(answer, encoding="utf-8")
                subprocess.run([*PYTHON, str(script), "reply", str(task), "--answer", str(output)],
                               cwd=project, check=True, capture_output=True)

            command = [*PYTHON, str(script), "run", "--project", str(project), "--host", "test",
                       "--history", history.name, "--tasks", str(tasks)]
            checked = subprocess.run([*command, "--validate-only"], cwd=root,
                                     check=True, capture_output=True, encoding="utf-8")
            self.assertEqual(json.loads(checked.stdout)["state"], "validated")
            self.assertFalse(tasks.exists())
            self.assertFalse(ignores.parent.exists())
            ignores.parent.mkdir(parents=True)
            ignores.write_bytes(b"# Previous local rule")
            with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8") as process:
                try:
                    deadline = monotonic() + 35
                    while process.poll() is None and monotonic() < deadline:
                        for task in tasks.glob("*/task.json"):
                            if not task.with_name("reply.json").exists():
                                reply_to_task(task)
                        sleep(0.05)
                    stdout, stderr = process.communicate(timeout=1)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertEqual(calls, ["context", "procedures", "consolidation", "knowledge", "review"])
            self.assertEqual(json.loads(stdout)["users"][0]["state"], "published")
            self.assertEqual(memory.read_text(encoding="utf-8"), "# Проверенное правило\n")
            self.assertFalse((project / "memory_pipeline.json").exists())
            self.assertEqual(ignores.read_bytes(), b"# Previous local rule\n*\n")
            repeated = subprocess.run([*command[:-1], str(root / "repeat")], cwd=root,
                                      check=True, capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(json.loads(repeated.stdout)["users"][0]["state"], "reused")
            self.assertEqual(ignores.read_bytes(), b"# Previous local rule\n*\n")

            if shutil.which("git"):
                environment = dict(os.environ)
                initialized = subprocess.run(["git", "init", "--quiet", "--template=", str(project)],
                                             cwd=root, env=environment, capture_output=True, encoding="utf-8")
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                status = subprocess.check_output(["git", "status", "--short", "--untracked-files=all"],
                                                 cwd=project, env=environment, encoding="utf-8")
                self.assertIn("?? AGENTS.md", status)
                self.assertNotIn(".agent-memory/runs/", status)

    def test_build_uses_current_sources(self):
        """Без входов сравнивает две сборки и проверяет приоритет исходного runtime и лицензию."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archives = []
            for checkout, newline in (("first", b"\n"), ("second", b"\r\n")):
                source = root / checkout
                skill = source / ".agents/skills/update-agent-memory"
                cached = skill / "scripts/runtime/command_agent_memory/memory_pipeline.py"
                current = source / "command_agent_memory/memory_pipeline.py"
                for file, contents in ((skill / "SKILL.md", b"# Skill" + newline),
                                       (cached, b"stale"), (current, b"# current" + newline),
                                       (source / "LICENSE", b"Test license" + newline)):
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(contents)
                builder = source / "scripts/build_memory_skill.py"
                builder.parent.mkdir()
                shutil.copyfile(REPOSITORY / "scripts/build_memory_skill.py", builder)
                archive = root / f"{checkout}.zip"
                subprocess.run([*PYTHON, str(builder), "--output", str(archive)],
                               check=True, capture_output=True)
                archives.append(archive)

            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())
            with ZipFile(archives[0]) as bundle:
                entry = "update-agent-memory/scripts/runtime/command_agent_memory/memory_pipeline.py"
                self.assertEqual(bundle.namelist().count(entry), 1)
                self.assertEqual(bundle.read(entry), b"# current\n")
                self.assertEqual(bundle.read("update-agent-memory/LICENSE"), b"Test license\n")

    def test_auto_history_uses_project_path(self):
        """Без входов проверяет выбор по абсолютному cwd, даже у одноимённых проектов."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            project = root / "main/project"
            project.mkdir(parents=True)
            history = root / "codex/sessions"
            history.mkdir(parents=True)

            def make_rollout(cwd: Path, session: str) -> None:
                """Принимает каталог и ID; сохраняет минимальную синтетическую историю Codex."""
                row = {"type": "session_meta", "payload": {"cwd": str(cwd), "id": session}}
                (history / f"rollout-{session}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

            make_rollout(project, "wanted")
            make_rollout(root / "other/project", "other")
            broken = history / "rollout-unrelated.jsonl"
            broken.write_text("{broken", encoding="utf-8")
            command = [*PYTHON, str(SKILL), "run", "--host", "codex", "--validate-only"]
            settings = {"cwd": project, "env": {**os.environ, "CODEX_HOME": str(history.parent)},
                        "capture_output": True, "encoding": "utf-8"}
            result = subprocess.run(command, check=True, **settings)
            self.assertIn("local / wanted", result.stderr)
            self.assertIn(broken.name, result.stderr)
            self.assertEqual(json.loads(result.stdout)["users"][0]["sessions"], 1)
            explicit = subprocess.run([*command, "--history", str(broken)], **settings)
            self.assertEqual(explicit.returncode, 1)
            self.assertIn("Повреждённый лог", json.loads(explicit.stdout)["error"])
            replay = subprocess.run([*command, "--evaluate-by-replay"], **settings)
            self.assertEqual(replay.returncode, 1)
            self.assertIn("Не передан сценарий", json.loads(replay.stdout)["error"])
            self.assertFalse((project / "AGENTS.md").exists())

    def test_reply_accepts_only_one_on_time(self):
        """Без входов моделирует два ответа и проверяет отказ после срока и завершения."""
        with tempfile.TemporaryDirectory() as name:
            task = Path(name) / "task.json"
            task.write_text(json.dumps({"expires_at": time() + 60}), encoding="utf-8")
            command = [*PYTHON, str(SKILL), "reply", str(task), "--error"]
            with subprocess.Popen([*command, "первый"], stderr=subprocess.PIPE) as first:
                with subprocess.Popen([*command, "второй"], stderr=subprocess.PIPE) as second:
                    first.communicate(timeout=10)
                    second.communicate(timeout=10)
            self.assertEqual(sorted((first.returncode, second.returncode)), [0, 1])
            reply = task.with_name("reply.json")
            winner = "первый" if first.returncode == 0 else "второй"
            self.assertEqual(json.loads(reply.read_bytes()), {"error": winner})
            reply.unlink()
            task.write_text(json.dumps({"expires_at": time() - 1}), encoding="utf-8")
            error = subprocess.run([*command, "поздний"], capture_output=True, encoding="utf-8")
            self.assertEqual(error.returncode, 1)
            self.assertIn("истёк", error.stderr)
            task.write_text(json.dumps({"expires_at": time() + 60}), encoding="utf-8")
            task.with_name("finished").touch()
            error = subprocess.run([*command, "лишний"], capture_output=True, encoding="utf-8")
            self.assertEqual(error.returncode, 1)
            self.assertIn("завершено", error.stderr)
            self.assertFalse(reply.exists())
