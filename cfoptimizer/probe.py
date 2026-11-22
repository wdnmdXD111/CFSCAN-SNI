from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import Counter

import aiohttp

from .models import Node
from .proxyip import ProxyContext, exit_type, normalize_ip
from .trace import MAX_TRACE_BYTES, USER_AGENT, is_trace_path, parse_trace
from .transport import cf_error, colo_from_headers, connector
from .util import bounded_map
from .zone_cache import ZoneCache

LOG = logging.getLogger(__name__)


async def verify(node: Node, cfg: dict, proxy: ProxyContext | None = None) -> tuple[Node | None, str]:
    url = f"{cfg['scheme']}://{cfg['domain']}:{node.endpoint.port}{cfg['path']}"
    trace_mode = is_trace_path(cfg["path"])
    if proxy is not None and not trace_mode:
        return None, "反代验证要求 zone.path=/cdn-cgi/trace"
    timeout = aiohttp.ClientTimeout(
        total=float(cfg["timeout_seconds"]),
        sock_connect=min(float(cfg["connect_timeout_seconds"]), float(cfg["timeout_seconds"])),
    )
    latencies = []
    exits = set()
    try:
        async with aiohttp.ClientSession(
            connector=connector(node.endpoint, cfg["verify_tls"]),
            timeout=timeout,
            trust_env=False,
            headers={"User-Agent": USER_AGENT, "Connection": "close"},
        ) as session:
            for _ in range(int(cfg["rounds"])):
                start = time.perf_counter()
                async with session.get(url, allow_redirects=False) as response:
                    header_latency = (time.perf_counter() - start) * 1000
                    try:
                        body = await response.content.readexactly(
                            MAX_TRACE_BYTES + 1 if trace_mode else 65536
                        )
                    except asyncio.IncompleteReadError as exc:
                        body = exc.partial
                    error = cf_error(body)
                    if error:
                        return None, error
                    if response.status not in cfg["accepted_statuses"]:
                        return None, f"HTTP {response.status}"
                    if trace_mode and response.status != 200:
                        return None, f"trace 要求 HTTP 200，实际 HTTP {response.status}"
                    if (cfg["require_cf_headers"] or trace_mode) and (
                        not response.headers.get("CF-Ray")
                        or "cloudflare" not in response.headers.get("Server", "").lower()
                    ):
                        return None, "未见 Cloudflare 响应头"
                    expected = cfg.get("expected_body", "")
                    if expected and expected.encode() not in body:
                        return None, "未匹配自定义 Zone 验证标记"
                    colo = colo_from_headers(response.headers)
                    if trace_mode:
                        trace, reason = parse_trace(
                            body, cfg["domain"], node.endpoint.port, cfg["scheme"], USER_AGENT, colo
                        )
                        if reason != "ok":
                            return None, reason
                        colo = trace.colo
                        if proxy is not None:
                            ip = normalize_ip(trace.exit_ip)
                            reason = proxy.check_exits([ip])
                            if reason != "ok":
                                return None, reason
                            exits.add(ip)
                    node.colo = colo
                    if cfg.get("allowed_colos") and node.colo not in cfg["allowed_colos"]:
                        return None, "机房不在 allowed_colos 中"
                    node.status = response.status
                    latencies.append(header_latency)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        return None, type(exc).__name__
    node.latency_ms = statistics.median(latencies)
    node.jitter_ms = statistics.pstdev(latencies)
    if proxy is not None:
        node.proxy_exit_ips = sorted(exits)
        node.proxy_exit_type = exit_type(node.proxy_exit_ips)
        node.proxy_risks = {}
    return node, "ok"


async def recheck_cached(
    node: Node, cfg: dict, cache: ZoneCache, proxy: ProxyContext | None = None
) -> tuple[Node | None, str]:
    """Refresh reused successes shortlisted for downloading or direct output."""
    if node.endpoint.address not in cache.reused:
        return node, "ok"
    cache.stats["rechecked"] += 1
    result, reason = await verify(node, cfg, proxy) if proxy is not None else await verify(node, cfg)
    cache.record(node, result, reason)
    if result is None:
        cache.stats["recheck_failed"] += 1
    cache.save()
    return result, reason


async def verify_all(
    nodes: list[Node], cfg: dict, cache: ZoneCache | None = None, proxy: ProxyContext | None = None
) -> tuple[list[Node], dict[str, str]]:
    done = 0
    last_log = time.monotonic()
    results = [None] * len(nodes)
    pending = []
    for index, node in enumerate(nodes):
        hit = cache.lookup(node) if cache is not None else None
        if hit is None:
            pending.append((index, node))
        else:
            results[index] = hit
    if cache is not None:
        if cache.enabled:
            LOG.info(
                "Zone 缓存：通过命中 %d，失败命中 %d，需实际验证 %d/%d",
                cache.stats["hit_passed"],
                cache.stats["hit_rejected"],
                len(pending),
                len(nodes),
            )
        else:
            LOG.info("Zone 缓存已关闭，实际验证全部 %d 个入选候选", len(nodes))

    async def worker(item):
        nonlocal done, last_log
        index, node = item
        result = await verify(node, cfg, proxy) if proxy is not None else await verify(node, cfg)
        results[index] = result
        if cache is not None:
            cache.stats["fresh"] += 1
            cache.record(node, *result)
            cache.save()
        done += 1
        if done == len(pending) or time.monotonic() - last_log > 3:
            LOG.info("Zone 实际验证 %d/%d", done, len(pending))
            last_log = time.monotonic()
        return result

    try:
        await bounded_map(worker, pending, cfg["concurrency"])
    finally:
        if cache is not None:
            cache.save(force=True)
    passed = [node for node, _ in results if node]
    rejected = {
        original.endpoint.address: reason for original, (node, reason) in zip(nodes, results) if node is None
    }
    LOG.info("Zone 通过 %d/%d，拒绝原因 %s", len(passed), len(nodes), dict(Counter(rejected.values())))
    return passed, rejected
