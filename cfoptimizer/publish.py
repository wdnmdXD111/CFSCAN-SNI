from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .util import OptimizerError, resolve_path, write_json

LOG = logging.getLogger(__name__)


@dataclass
class SyncResult:
    success: bool
    message: str
    local_sha: str = ""
    remote_sha: str = ""


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"https://[^/@\s]+@", "https://[REDACTED]@", text)
    return text.strip()[-3000:]


class Git:
    def __init__(self, directory: Path, timeout: float, token: str = ""):
        self.directory = directory
        self.timeout = timeout
        self.set_token(token)

    def set_token(self, token: str) -> None:
        self.token = token
        self.encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode() if token else ""

    def call(
        self, *args: str, authenticate: bool = False, okay: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
        environment.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        if authenticate and self.token:
            # Environment config keeps credentials out of argv, .git/config and remote URLs.
            # Restrict the header to GitHub HTTPS rather than every possible HTTP remote.
            count = int(environment.get("GIT_CONFIG_COUNT", "0"))
            environment[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraHeader"
            environment[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {self.encoded}"
            environment["GIT_CONFIG_COUNT"] = str(count + 1)
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.directory,
                env=environment,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OptimizerError(f"git {args[0]} 超时，不能判定成功") from exc
        except OSError as exc:
            raise OptimizerError("找不到 Git 或 Git 进程无法运行") from exc
        if result.returncode not in okay:
            detail = redact(result.stderr or result.stdout, [self.token, self.encoded])
            raise OptimizerError(f"git {args[0]} 失败（{result.returncode}）: {detail}")
        return result


def publish(output: Path, cfg: dict, base: Path) -> SyncResult:
    """Commit exactly the output path and require an independently read remote SHA."""
    if not cfg["enabled"]:
        return SyncResult(False, "disabled")
    status_path = resolve_path(base, cfg["status_file"])
    started = datetime.now(timezone.utc).isoformat()
    # Invalidate a prior successful status before attempting any side effect.
    write_json(status_path, {"success": False, "state": "running", "attempted_at": started})
    git = Git(output.parent, float(cfg["timeout_seconds"]))
    result = SyncResult(False, "尚未确认远端")
    try:
        root = git.call("rev-parse", "--show-toplevel").stdout.strip()
        git.directory = Path(root)
        configured_token = git.call("config", "--get", "cfscan.githubToken", okay=(0, 1)).stdout.strip()
        git.set_token(configured_token)
        try:
            relative = output.resolve().relative_to(git.directory.resolve()).as_posix()
        except ValueError as exc:
            raise OptimizerError("结果文件不在 Git 仓库内") from exc
        if not output.is_file():
            raise OptimizerError("结果文件不存在")
        remote = str(cfg["remote"])
        if not remote or remote.startswith("-"):
            raise OptimizerError("github.remote 必须是已配置的远端名称")
        remote_url = git.call("remote", "get-url", "--push", remote).stdout.strip()
        # Disallow embedded credentials so they are never persisted or echoed.
        if urlsplit(remote_url).scheme in ("https", "http") and urlsplit(remote_url).username:
            raise OptimizerError("远端 URL 包含凭证，请改成无凭证 URL，并通过 git config 或 SSH 配置认证")
        branch = str(cfg["branch"]).strip() or git.call("branch", "--show-current").stdout.strip()
        if not branch:
            raise OptimizerError("当前为 detached HEAD，请配置 github.branch")
        git.call("check-ref-format", "--branch", branch)
        # Only this path enters the commit; unrelated staged changes stay staged.
        git.call("add", "--", relative)
        changed = git.call("diff", "--cached", "--quiet", "--", relative, okay=(0, 1)).returncode
        if changed:
            git.call("commit", "--only", "-m", str(cfg["commit_message"]), "--", relative)
        expected = git.call("hash-object", "--", relative).stdout.strip()
        committed = git.call("rev-parse", f"HEAD:{relative}").stdout.strip()
        if expected != committed:
            raise OptimizerError("HEAD 中的结果与当前文件不一致，拒绝推送")
        result.local_sha = git.call("rev-parse", "HEAD").stdout.strip()
        retries = int(cfg["retries"])
        for attempt in range(1, retries + 1):
            try:
                git.call("push", "--porcelain", remote, f"HEAD:refs/heads/{branch}", authenticate=True)
                # Read the PUSH destination, not fetch origin (they can differ).
                # No raw.githubusercontent.com cache and no reliance on console text.
                remote_result = git.call(
                    "ls-remote", "--exit-code", remote_url, f"refs/heads/{branch}", authenticate=True
                )
                rows = [row.split() for row in remote_result.stdout.splitlines()]
                result.remote_sha = next(
                    (row[0] for row in rows if len(row) == 2 and row[1] == f"refs/heads/{branch}"), ""
                )
                if result.remote_sha != result.local_sha:
                    raise OptimizerError("push 返回成功，但远端分支 SHA 与本地提交不一致")
                # Catch concurrent local mutation; verified HEAD must still contain
                # the exact output snapshot being reported as published.
                if git.call("hash-object", "--", relative).stdout.strip() != expected:
                    raise OptimizerError("推送期间结果文件被其他进程修改，不能确认当前文件已发布")
                result.success = True
                result.message = f"远端已确认 {branch}@{result.local_sha[:12]}"
                break
            except OptimizerError as exc:
                result.message = redact(str(exc), [git.token, git.encoded])
                LOG.warning("GitHub 同步 %d/%d：%s", attempt, retries, result.message)
                if attempt < retries:
                    time.sleep(float(cfg["retry_delay_seconds"]))
    except (OptimizerError, OSError, ValueError) as exc:
        result.message = redact(str(exc), [git.token, git.encoded])
    write_json(
        status_path,
        {**asdict(result), "attempted_at": started, "finished_at": datetime.now(timezone.utc).isoformat()},
    )
    if result.success:
        LOG.info("GitHub 同步成功：%s", result.message)
    else:
        LOG.error("GitHub 同步失败：%s", result.message)
    return result
