import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfoptimizer.config import DEFAULTS
from cfoptimizer.publish import Git, publish
from cfoptimizer.util import OptimizerError


@unittest.skipUnless(shutil.which("git"), "Git required")
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "local"
        self.repo.mkdir()
        self.remote = self.base / "remote.git"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Optimizer tests")
        self.git("config", "user.email", "optimizer@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        (self.repo / "README.md").write_text("test repo\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "initial")
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.output = self.repo / "ip.txt"
        self.output.write_text("1.2.3.4:443#JP 日本\n", encoding="utf-8")
        self.cfg = copy.deepcopy(DEFAULTS["github"])
        self.cfg.update(enabled=True, branch="main", retries=1, retry_delay_seconds=0)

    def tearDown(self):
        # Git object files can be read-only on Windows.
        def writable(function, path, exc):
            os.chmod(path, 0o700)
            function(path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(self.base, onexc=writable)
        else:
            shutil.rmtree(self.base, onerror=writable)
        self.temporary.cleanup()

    def git(self, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip()

    def status(self):
        return json.loads((self.repo / self.cfg["status_file"]).read_text(encoding="utf-8"))

    def reject_push(self):
        path = self.remote / "hooks" / "pre-receive"
        path.write_text("#!/bin/sh\necho 'test rejection' >&2\nexit 1\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
        return path

    def test_only_output_is_committed_and_remote_is_verified(self):
        (self.repo / "unrelated.txt").write_text("not for publishing")
        self.git("add", "unrelated.txt")
        result = publish(self.output, self.cfg, self.repo)
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.local_sha, result.remote_sha)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")
        self.assertNotIn("unrelated.txt", self.git("ls-tree", "--name-only", "HEAD"))
        self.assertTrue(self.status()["success"])

    def test_token_comes_from_git_config_and_covers_push_and_verification(self):
        self.git("config", "--local", "cfscan.githubToken", "configured-test-token")
        authenticated = []
        original = Git.call

        def observe(git, *args, **kwargs):
            if kwargs.get("authenticate"):
                authenticated.append((args[0], git.token))
            return original(git, *args, **kwargs)

        with patch.object(os, "environ", dict(os.environ, GITHUB_TOKEN="ignored-environment-token")):
            with patch.object(Git, "call", observe):
                result = publish(self.output, self.cfg, self.repo)
        self.assertTrue(result.success, result.message)
        self.assertEqual(
            authenticated,
            [("push", "configured-test-token"), ("ls-remote", "configured-test-token")],
        )
        self.assertNotIn("configured-test-token", json.dumps(self.status()))

    def test_environment_token_is_not_used_when_git_config_is_empty(self):
        self.git("config", "--local", "cfscan.githubToken", "")
        observed = []
        original = Git.call

        def observe(git, *args, **kwargs):
            if kwargs.get("authenticate"):
                observed.append(git.token)
            return original(git, *args, **kwargs)

        with patch.object(os, "environ", dict(os.environ, GITHUB_TOKEN="ignored-environment-token")):
            with patch.object(Git, "call", observe):
                result = publish(self.output, self.cfg, self.repo)
        self.assertTrue(result.success, result.message)
        self.assertEqual(observed, ["", ""])

    def test_git_config_token_is_redacted_on_failure(self):
        self.git("config", "--local", "cfscan.githubToken", "private-test-token")
        original = Git.call

        def fail(git, *args, **kwargs):
            if args[0] == "push":
                raise OptimizerError(f"test failure {git.token} {git.encoded}")
            return original(git, *args, **kwargs)

        with patch.object(Git, "call", fail):
            result = publish(self.output, self.cfg, self.repo)
        self.assertFalse(result.success)
        self.assertNotIn("private-test-token", result.message)
        self.assertIn("[REDACTED]", result.message)
        self.assertNotIn("private-test-token", json.dumps(self.status()))

    def test_first_result_can_initialize_history_without_source_or_empty_commit(self):
        fresh = self.base / "fresh"
        fresh.mkdir()
        remote = self.base / "fresh-remote.git"

        def git(*args):
            return subprocess.run(["git", *args], cwd=fresh, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.name", "Optimizer tests")
        git("config", "user.email", "optimizer@example.invalid")
        git("config", "commit.gpgsign", "false")
        git("config", "cfscan.githubToken", "")
        git("init", "--bare", str(remote))
        git("remote", "add", "origin", str(remote))
        (fresh / "main.py").write_text("not published\n")
        output = fresh / "ip.txt"
        output.write_text("1.2.3.4:443#JP 日本\n", encoding="utf-8")
        result = publish(output, self.cfg, fresh)
        self.assertTrue(result.success, result.message)
        self.assertEqual(git("ls-tree", "--name-only", "HEAD").stdout.decode().strip(), "ip.txt")

    def test_rejected_push_is_failure_and_pending_commit_can_retry(self):
        hook = self.reject_push()
        result = publish(self.output, self.cfg, self.repo)
        self.assertFalse(result.success)
        self.assertFalse(self.status()["success"])
        pending_head = self.git("rev-parse", "HEAD")
        hook.unlink()
        # File unchanged: still must push the previously rejected commit.
        retried = publish(self.output, self.cfg, self.repo)
        self.assertTrue(retried.success, retried.message)
        self.assertEqual(pending_head, retried.remote_sha)

    def test_zero_exit_with_wrong_remote_sha_is_not_success(self):
        original = Git.call

        def fake(git, *args, **kwargs):
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess([], 0, "0" * 40 + "\trefs/heads/main\n", "")
            return original(git, *args, **kwargs)

        with patch.object(Git, "call", fake):
            result = publish(self.output, self.cfg, self.repo)
        self.assertFalse(result.success)
        self.assertIn("SHA", result.message)
        self.assertFalse(self.status()["success"])

    def test_verification_timeout_overwrites_prior_success(self):
        initial = publish(self.output, self.cfg, self.repo)
        self.assertTrue(initial.success)
        original = Git.call

        def fake(git, *args, **kwargs):
            if args[0] == "ls-remote":
                raise OptimizerError("verification timeout")
            return original(git, *args, **kwargs)

        with patch.object(Git, "call", fake):
            result = publish(self.output, self.cfg, self.repo)
        self.assertFalse(result.success)
        self.assertFalse(self.status()["success"])

    def test_push_url_is_verified_instead_of_fetch_url(self):
        other = self.base / "push-target.git"
        subprocess.run(["git", "init", "--bare", str(other)], check=True, capture_output=True)
        self.git("remote", "set-url", "--push", "origin", str(other))
        result = publish(self.output, self.cfg, self.repo)
        self.assertTrue(result.success, result.message)
        fetch_sha = self.git("ls-remote", str(self.remote), "refs/heads/main").split()[0]
        self.assertNotEqual(fetch_sha, result.local_sha)
