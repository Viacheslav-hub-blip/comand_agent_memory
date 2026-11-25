"""Проверки Git-доставки на локальном bare-репозитории без внешних сервисов.

GitDeliveryTests проверяет доставку, кэш, восстановление после сбоя и целостность.
Методы setUp, git, configuration и run_mode готовят и запускают сценарии.
test_first_delivery_duplicate_and_offline_snapshot проверяет доставку и миграцию метки.
test_same_contribution_from_new_cache_keeps_remote_manifest проверяет повторный вклад.
test_recovers_lost_push_response_and_recomputes_stale_proposal проверяет сбои и гонку.
Вложенная функция interrupt имитирует сбой push.
test_rejects_changed_package_without_model_call проверяет отклонение повреждённого пакета.
test_sender_sees_resolution проверяет доставку вопроса владельцу вклада.
test_reads_unique_blobs_in_one_batch проверяет пакетное чтение байтов.
ContributionSafetyTests содержит test_screen_text_rejects_credentials_in_structured_json
и test_git_error_explains_failure_without_returning_stderr.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_memory_pipeline as fixture
from command_agent_memory.memory_pipeline import run_pipeline
from command_agent_memory.team_memory_delivery.contributions import screen_text
from command_agent_memory.team_memory_delivery.git_repository import GitCommandError, GitRepository
from command_agent_memory.team_memory_delivery.git_team import (
    CONTRIBUTIONS, GitTeamConfig, read_team_version, run_git_team,
)


class GitDeliveryTests(unittest.TestCase):
    """Запускает настоящий Git-транспорт с локальной модельной имитацией."""

    def setUp(self):
        """Без входов создаёт изолированные репозитории и каталоги участников."""
        self.model = fixture.ConnectionTests()
        self.model.setUp()
        self.addCleanup(self.model.tearDown)
        self.root = self.model.root
        self.remote = self.root / "remote.git"
        self.recipient = self.root / "recipient"
        self.recipient.mkdir()
        (self.recipient / "AGENTS.md").write_text("Личные правила получателя.", encoding="utf-8")
        self.git("init", "--quiet", "--bare", "--template=", "--initial-branch=main", str(self.remote))
        seed = self.root / "seed"
        self.git("init", "--quiet", "--template=", "--initial-branch=main", str(seed))
        self.git("-C", str(seed), "commit", "--quiet", "--allow-empty", "--no-gpg-sign",
                 "--no-verify", "-m", "Seed")
        self.git("-C", str(seed), "push", "--quiet", str(self.remote), "main")

    def git(self, *args: str) -> str:
        """Принимает аргументы Git; возвращает декодированный stdout команды."""
        result = subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                 *args], cwd=self.root, env=dict(os.environ), capture_output=True,
                                check=False, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def configuration(self, role: str) -> dict:
        """Принимает роль; возвращает конфигурацию с отдельным кэшем рабочего места."""
        values = {"defaults": {"provider": "fake-a", "model": "model-a"},
                  "team": {"type": "git", "team_id": "team-test", "repository": str(self.remote),
                           "cache_dir": role + "-cache", "export_paths": []}}
        if role == "member":
            values["users"] = self.model.configuration["users"]
            values["team"]["export_paths"] = ["notes"]
        if role == "recipient":
            values["team"]["workspaces"] = ["recipient"]
        return values

    def run_mode(self, role: str, mode: str | None = None, *, validate_only: bool = False) -> dict:
        """Принимает роль и режим; возвращает структурированный результат настоящего конвейера."""
        path = self.root / f"{role}.json"
        path.write_text(json.dumps(self.configuration(role)), encoding="utf-8")
        return run_pipeline(path, team_mode=mode, validate_only=validate_only, raise_on_failure=False)

    def test_first_delivery_duplicate_and_offline_snapshot(self):
        """Проверяет путь от принятого личного снимка до закреплённой командной версии."""
        self.assertEqual(self.run_mode("member", validate_only=True)["state"], "validated")
        self.assertFalse((self.root / "member-cache").exists())
        publication = self.run_mode("member")
        self.assertEqual(publication["users"][0]["state"], "published")
        contribution = publication["team"]["contributions"][0]
        self.assertEqual(contribution["state"], "submitted", publication)
        awaiting, = self.run_mode("member", "status")["team"]["contributions"]
        self.assertEqual((awaiting["id"], awaiting["state"]), (contribution["id"], "awaiting_processing"))
        before = self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main")
        self.assertEqual(before, "")
        self.assertEqual(self.run_mode("member")["users"][0]["state"], "reused")
        applied = self.run_mode("executor", "process")
        self.assertEqual(applied["team"]["contributions"][0]["state"], "applied", applied)
        status = self.run_mode("member", "status")["team"]
        self.assertEqual(status["commit"], applied["team"]["contributions"][0]["commit"])
        published, = status["contributions"]
        self.assertEqual(published["state"], "applied")
        self.assertEqual([item["status"] for item in published["knowledge"]], ["accepted"])
        again = self.run_mode("executor", "process")
        self.assertTrue(again["team"]["contributions"][0]["reused"], again)
        self.assertEqual(sum(call[0] == "team" for call in self.model.calls), 1)
        delivered = self.run_mode("recipient", "sync")
        sha = delivered["team"]["commit"]
        self.assertEqual(delivered["team"]["state"], "synced")
        self.assertEqual((self.recipient / "team_knowledge/wiki/procedure.md").read_text(encoding="utf-8"),
                         "Сохранённая процедура")
        self.assertFalse((self.recipient / "team_knowledge/.registry").exists())
        self.assertEqual((self.recipient / "AGENTS.md").read_text(encoding="utf-8"), "Личные правила получателя.")
        marker = self.recipient / "team_knowledge/.team-version.json"
        previous = json.loads(marker.read_bytes())
        previous.pop("document_sha256")
        marker.write_text(json.dumps(previous), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--team-sync"):
            with read_team_version(self.recipient):
                self.fail("Метка без хешей должна требовать синхронизации")
        with patch.object(GitRepository, "fetch", side_effect=GitCommandError("Связь недоступна")):
            cached = self.run_mode("recipient", "sync")
        self.assertEqual((cached["team"]["state"], cached["team"]["commit"]), ("cached", sha))

        repo = GitRepository(self.root / "fixture-objects.git", str(self.remote))
        self.assertEqual(repo.fetch("refs/heads/main"), sha)
        advanced = repo.commit(sha, repo.tree(sha), "Another version")
        repo.push(advanced, "refs/heads/main")
        with read_team_version(self.recipient) as pinned:
            self.assertEqual(pinned, sha)
            with self.assertRaisesRegex(RuntimeError, "обрабатывается"):
                self.run_mode("recipient", "sync")
        self.assertEqual(self.run_mode("recipient", "sync")["team"]["commit"], advanced)

    def test_sender_sees_resolution(self):
        """Без входов проверяет, что при отказе отправитель получает конкретное уточнение и пустой список знаний."""
        self.run_mode("member")
        decision = {"decision": "needs_resolution", "summary": "Нужна версия определения.",
                    "findings": [{"file": "wiki/metric.md", "problem": "Две версии",
                                  "change": "Уточните дату перехода."}], "updates": []}
        with patch.object(self.model, "runner", return_value=json.dumps(decision)):
            self.run_mode("executor", "process")
        row, = self.run_mode("member", "status")["team"]["contributions"]
        self.assertEqual(row["state"], "needs_resolution")
        self.assertIn("Уточните дату перехода.", row["resolution"])
        self.assertEqual(row["knowledge"], [])

    def test_same_contribution_from_new_cache_keeps_remote_manifest(self):
        """Отправляет те же документы из нового кэша и проверяет сохранение первого commit."""
        initial = self.run_mode("member")
        first = initial["team"]["contributions"][0]
        original = Path(initial["users"][0]["run"])
        repeated = self.root / "repeat-run" / "personal"
        shutil.copytree(original, repeated)
        status = repeated / "status.json"
        metadata = json.loads(status.read_bytes())
        metadata["replay_status"] = "skipped"
        status.write_text(json.dumps(metadata), encoding="utf-8")
        settings = {**self.configuration("member")["team"], "cache_dir": "new-cache"}
        config = GitTeamConfig.from_json(settings, self.root, [])
        result = {}
        run_git_team(config, "contribute", [repeated], [], result)
        self.assertEqual(result["contributions"], [first])
        self.assertTrue((config.cache_dir / "outbox" / f"{first['id']}.sent").is_file())
        outcome = self.run_mode("executor", "process")
        self.assertEqual(outcome["team"]["contributions"][0]["state"], "applied")
        repeat = self.run_mode("executor", "process")
        self.assertTrue(repeat["team"]["contributions"][0]["reused"])
        self.assertEqual(sum(call[0] == "team" for call in self.model.calls), 1)

    def test_recovers_lost_push_response_and_recomputes_stale_proposal(self):
        """Проверяет подтверждение после потери ответа и повтор относительно новой версии."""
        original_push = GitRepository.push
        anomalies = {"contribution": True, "race": True, "receipt": True}

        def interrupt(repo, commit, ref):
            """Принимает параметры push; воспроизводит гонку и потерю ответа, возвращает None."""
            if ref.startswith(CONTRIBUTIONS) and anomalies["contribution"]:
                anomalies["contribution"] = False
                original_push(repo, commit, ref)
                raise GitCommandError("Ответ отправки вклада потерян.")
            if ref == "refs/heads/main":
                if anomalies["race"]:
                    anomalies["race"] = False
                    latest = repo.fetch(ref)
                    moved = repo.commit(latest, repo.tree(latest), "Concurrent commit")
                    original_push(repo, moved, ref)
                elif anomalies["receipt"]:
                    anomalies["receipt"] = False
                    original_push(repo, commit, ref)
                    raise GitCommandError("Ответ отправки квитанции потерян.")
            original_push(repo, commit, ref)

        with patch.object(GitRepository, "push", interrupt):
            submitted = self.run_mode("member")
            self.assertEqual(submitted["team"]["contributions"][0]["state"], "submitted", submitted)
            processed = self.run_mode("executor", "process")
            self.assertEqual(processed["state"], "completed", processed)
            outcome = processed["team"]["contributions"][0]
            self.assertTrue(outcome["reused"])
            self.assertEqual(outcome["state"], "applied")
        self.assertEqual(sum(call[0] == "team" for call in self.model.calls), 2)

    def test_rejects_changed_package_without_model_call(self):
        """Отклоняет пакет с изменённым файлом и сохраняет исходную каноническую ветку."""
        submitted = self.run_mode("member")
        identifier = submitted["team"]["contributions"][0]["id"]
        repo = GitRepository(self.root / "fixture-objects.git", str(self.remote))
        base = repo.fetch("refs/heads/main")
        original = repo.fetch(CONTRIBUTIONS + identifier)
        files = repo.tree(original)
        manifest_path = f"contributions/{identifier}/manifest.json"
        documents = [name for name in files if name != manifest_path]
        files[documents[0]] = b"modified bytes"
        changed = repo.commit(base, files, "Invalid package")
        repo.run("update-ref", "refs/heads/damaged-fixture", changed)
        self.git("--git-dir", str(self.remote), "fetch", str(repo.cache), "refs/heads/damaged-fixture")
        self.git("--git-dir", str(self.remote), "update-ref", CONTRIBUTIONS + identifier, changed, original)
        result = self.run_mode("executor", "process")
        self.assertEqual(result["state"], "failed")
        self.assertIn("Хеши документов", result["team"]["contributions"][0]["error"])
        self.assertFalse(any(call[0] == "team" for call in self.model.calls))
        self.assertEqual(repo.fetch("refs/heads/main"), base)


    def test_reads_unique_blobs_in_one_batch(self):
        """Проверяет чтение пустых, повторяющихся и бинарных Git-объектов за один пакет."""
        repo = GitRepository(self.root / "blob-cache.git", str(self.remote))
        base = repo.fetch("refs/heads/main")
        files = {"first.txt": b"", "another.txt": b"", "binary.txt": b"data\x00\nmore\n"}
        commit = repo.commit(base, files, "Batch fixture")
        with patch.object(repo, "run", wraps=repo.run) as git:
            self.assertEqual(repo.tree(commit), files)
        self.assertEqual([call.args[0] for call in git.call_args_list], ["ls-tree", "cat-file"])
        self.assertEqual(len(git.call_args.kwargs["input_data"].splitlines()), 2)


class ContributionSafetyTests(unittest.TestCase):
    """Проверяет фильтр текстового экспорта и вывод диагностики без внешних сервисов."""

    def test_screen_text_rejects_credentials_in_structured_json(self):
        """Проверяет заполненные поля внутри объектов и списков, включая экранированные ключи."""
        for text in (
            '{"config": [{"password": "demo"}]}',
            '{"api_key": "demo"}',
            '{"APIKey": "demo", "APIKey": null}',
            r'{"api\u005fkey": "demo"}',
            'Пример настройки: {"password": "fictional-long-value"}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                screen_text(text.encode("utf-8"))
        screen_text(b'{"api_key": "", "password": null, "api_key_env": "APP_KEY", "timeout": 10}')

    def test_git_error_explains_failure_without_returning_stderr(self):
        """Имитирует ошибку Git. Проверяет причину и скрытие исходного сообщения."""
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            repo = GitRepository(Path("objects.git"), "https://example.test/project")
            run.return_value.returncode = 128
            run.return_value.stderr = b"fatal: Authentication failed for 'fictional-private-remote'"
            with self.assertRaises(GitCommandError) as failure:
                repo.fetch("refs/heads/main")
        self.assertIn("учётные данные Git", str(failure.exception))
        self.assertNotIn("fictional-private-remote", str(failure.exception))
