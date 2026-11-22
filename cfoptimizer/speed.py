from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .models import Node
from .transport import cf_error, connector, pinned_url
from .util import OptimizerError, read_tokens, resolve_path

LOG = logging.getLogger(__name__)


class InvalidSample(Exception):
    pass


@dataclass
class Transfer:
    limit: int
    received: int = 0
    valid_bytes: int = 0


def token_from_config(cfg: dict, base: Path) -> str:
    if cfg["mode"] != "worker" or not cfg["token_file"]:
        return ""
    tokens = read_tokens(resolve_path(base, cfg["token_file"]))
    if len(tokens) > 1 or any(char.isspace() for token in tokens for char in token):
        raise OptimizerError("speed.token_file 只允许填写一行测速口令；无需口令时留空")
    return tokens[0] if tokens else ""


async def sample(node: Node, cfg: dict, verify_tls: bool, token: str = "") -> tuple[float, int, str]:
    streams = int(cfg["streams"])
    transfer = Transfer(int(cfg["max_bytes"]))
    min_bytes = int(cfg["min_bytes"])
    url = pinned_url(cfg["url"], node.endpoint.port)
    headers = {
        "User-Agent": "CFSCAN-SNI/1.0",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Range": f"bytes=0-{transfer.limit - 1}",
    }
    if cfg["mode"] == "worker" and token:
        headers["X-Speed-Token"] = token
        parts = urlsplit(url)
        query = [
            (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "token"
        ]
        query.append(("token", token))
        url = urlunsplit(parts._replace(query=urlencode(query)))
    duration = float(cfg["duration_seconds"])
    start = time.perf_counter()
    deadline = start + duration
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=float(cfg["connect_timeout_seconds"]),
        sock_read=float(cfg["timeout_seconds"]),
    )
    reasons: list[str] = []
    async with aiohttp.ClientSession(
        connector=connector(node.endpoint, verify_tls, streams),
        trust_env=False,
        headers=headers,
        timeout=timeout,
        auto_decompress=False,
    ) as session:

        async def stream():
            while transfer.received < transfer.limit and time.perf_counter() < deadline:
                response_bytes = 0
                prefix = bytearray()
                valid_header = False
                cancelled = False
                try:
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status not in (200, 206):
                            raise InvalidSample(f"下载 HTTP {response.status}（不跟随重定向）")
                        mime = response.headers.get("Content-Type", "").lower().split(";", 1)[0].strip()
                        if mime in ("text/html", "application/json", "application/xml", "text/xml"):
                            raise InvalidSample(f"拒绝错误页类型 {mime}")
                        encoding = response.headers.get("Content-Encoding", "identity").lower()
                        if encoding not in ("identity", ""):
                            raise InvalidSample("拒绝压缩响应，避免口径失真")
                        if cfg["mode"] == "worker" and not (
                            response.headers.get("X-Upstream-Mode")
                            and response.headers.get("X-Upstream-Landed")
                        ):
                            raise InvalidSample("Worker 缺少回源确认头，检查 /__speed 是否部署")
                        if response.content_length is not None and response.content_length < min_bytes:
                            raise InvalidSample("下载正文不足最小有效字节数")
                        valid_header = True
                        while transfer.received < transfer.limit:
                            remaining = transfer.limit - transfer.received
                            chunk = await response.content.read(min(65536, remaining))
                            if not chunk:
                                break
                            # No await between budget read and increment: shared cap is atomic
                            # in this event loop, including all streams.
                            chunk = chunk[: transfer.limit - transfer.received]
                            transfer.received += len(chunk)
                            response_bytes += len(chunk)
                            if len(prefix) < 8192:
                                prefix.extend(chunk[: 8192 - len(prefix)])
                                error = cf_error(prefix)
                                if error:
                                    raise InvalidSample(error)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except InvalidSample as exc:
                    reasons.append(str(exc))
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    reasons.append(type(exc).__name__)
                    return
                finally:
                    if valid_header:
                        # Small tail streams can occur when another stream uses the shared
                        # budget; accepting the aggregate is safe after an individual body
                        # has reached min_bytes. Tiny responses cannot be looped into validity.
                        if response_bytes >= min_bytes:
                            transfer.valid_bytes += response_bytes
                        elif response_bytes and not cancelled and transfer.received < transfer.limit:
                            reasons.append(
                                f"单次下载正文不足：收到 {response_bytes} 字节，"
                                f"最低要求 {min_bytes} 字节，本轮测速无效"
                            )
                if response_bytes < min_bytes:
                    return

        tasks = [asyncio.create_task(stream()) for _ in range(streams)]
        try:
            _, pending = await asyncio.wait(tasks, timeout=duration)
            for task in pending:
                task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    reasons.append(type(outcome).__name__)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - start
    if reasons:
        return 0.0, transfer.received, "; ".join(sorted(set(reasons)))
    if transfer.valid_bytes < min_bytes or elapsed <= 0:
        return 0.0, transfer.received, "下载有效字节不足"
    return transfer.valid_bytes * 8 / elapsed / 1_000_000, transfer.valid_bytes, "ok"


async def measure(node: Node, cfg: dict, verify_tls: bool, base: Path) -> tuple[Node | None, str]:
    token = token_from_config(cfg, base)
    speeds = []
    received = []
    for index in range(int(cfg["repeats"])):
        mbps, byte_count, reason = await sample(node, cfg, verify_tls, token)
        if reason != "ok":
            return None, f"第 {index + 1} 次测速失败: {reason}"
        speeds.append(mbps)
        received.append(byte_count)
        LOG.info(
            "%s %s 测速 %d/%d：%.2f Mbps",
            node.region_code or node.geo.country_code,
            node.endpoint.address,
            index + 1,
            int(cfg["repeats"]),
            mbps,
        )
    node.speed_mbps = statistics.median(speeds)
    node.bytes_received = sum(received)
    node.speed_samples = speeds
    if node.speed_mbps < float(cfg["min_mbps"]):
        return None, "速度低于 min_mbps"
    return node, "ok"
