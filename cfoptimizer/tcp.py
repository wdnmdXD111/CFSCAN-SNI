from __future__ import annotations

import asyncio
import logging
import socket
import statistics
import time
from collections import Counter

from .models import Endpoint, Node
from .util import bounded_map

LOG = logging.getLogger(__name__)


async def connect(endpoint: Endpoint, timeout: float) -> float:
    """Time one TCP handshake to the literal endpoint, without TLS or HTTP."""
    family = socket.AF_INET6 if ":" in endpoint.ip else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.setblocking(False)
        start = time.perf_counter()
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(connection, (endpoint.ip, endpoint.port)), timeout
        )
        return (time.perf_counter() - start) * 1000


async def verify(node: Node, cfg: dict) -> tuple[Node | None, str]:
    samples = []
    last_error = ""
    node.tcp_attempts = int(cfg["rounds"])
    for _ in range(node.tcp_attempts):
        try:
            samples.append(await connect(node.endpoint, float(cfg["timeout_seconds"])))
        except (OSError, asyncio.TimeoutError) as exc:
            last_error = type(exc).__name__
    node.tcp_successes = len(samples)
    if not samples:
        return None, f"TCP 无成功连接 ({last_error})"
    node.tcp_latency_ms = max(samples)
    node.tcp_jitter_ms = statistics.pstdev(samples)
    if node.tcp_successes / node.tcp_attempts < float(cfg["min_success_rate"]):
        return None, f"TCP 成功率不足：{node.tcp_successes}/{node.tcp_attempts}"
    return node, "ok"


async def verify_all(nodes: list[Node], cfg: dict) -> tuple[list[Node], dict[str, str]]:
    done = 0
    last_log = time.monotonic()
    LOG.info(
        "开始 TCP 测试，共 %d 个候选，并发 %d，每个 %d 轮", len(nodes), cfg["concurrency"], cfg["rounds"]
    )

    async def worker(node):
        nonlocal done, last_log
        result = await verify(node, cfg)
        done += 1
        if done == len(nodes) or time.monotonic() - last_log > 3:
            LOG.info("TCP 测试 %d/%d", done, len(nodes))
            last_log = time.monotonic()
        return result

    results = await bounded_map(worker, nodes, cfg["concurrency"])
    passed = [node for node, _ in results if node is not None]
    rejected = {
        original.endpoint.address: reason for original, (node, reason) in zip(nodes, results) if node is None
    }
    LOG.info("TCP 通过 %d/%d，拒绝原因 %s", len(passed), len(nodes), dict(Counter(rejected.values())))
    return passed, rejected
