from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

import aiohttp

from .models import Node
from .trace import MAX_TRACE_BYTES, USER_AGENT, parse_trace
from .transport import colo_from_headers
from .util import OptimizerError

LOG = logging.getLogger(__name__)
EXIT_TYPES = ("ipv4_only", "dual_stack", "ipv6_only")


def normalize_ip(value: str) -> str:
    if not isinstance(value, str) or "%" in value:
        raise ValueError("invalid IP address")
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def exit_type(exits: list[str]) -> str:
    families = {ipaddress.ip_address(normalize_ip(ip)).version for ip in exits}
    return {frozenset({4}): "ipv4_only", frozenset({6}): "ipv6_only"}.get(
        frozenset(families), "dual_stack" if families == {4, 6} else ""
    )


class ProxyContext:
    """Same-family direct exit samples, kept in memory for one run only."""

    def __init__(self, baselines: dict[int, set[str]], requests: int = 0):
        self.baselines = {
            family: {normalize_ip(ip) for ip in ips} for family, ips in baselines.items() if ips
        }
        if any(
            ipaddress.ip_address(ip).version != family for family, ips in self.baselines.items() for ip in ips
        ):
            raise ValueError("baseline family mismatch")
        self.fingerprint = hashlib.sha256(
            json.dumps(
                {family: sorted(ips) for family, ips in self.baselines.items()}, sort_keys=True
            ).encode()
        ).hexdigest()
        self.stats = {
            "enabled": True,
            "baseline_requests": requests,
            "baseline_families": [f"ipv{family}" for family in sorted(self.baselines)],
        }

    def check_exits(self, exits: list[str]) -> str:
        if not isinstance(exits, list) or not exits:
            return "反代验证缺少已校验的出口 IP"
        for value in exits:
            try:
                ip = normalize_ip(value)
            except ValueError:
                return "反代出口 IP 格式无效"
            family = ipaddress.ip_address(ip).version
            if family not in self.baselines:
                return f"未取得 IPv{family} 直连出口基准，无法判断出口是否变化"
            if ip in self.baselines[family]:
                return f"IPv{family} 出口未变化，不符合反代验证要求"
        return "ok"


async def prepare(cfg: dict, zone: dict) -> ProxyContext:
    url = cfg["baseline_url"]
    parts = urlsplit(url)
    timeout = aiohttp.ClientTimeout(
        total=float(zone["timeout_seconds"]),
        sock_connect=min(float(zone["connect_timeout_seconds"]), float(zone["timeout_seconds"])),
    )
    requests = 0

    async def collect(version: int, family: int) -> tuple[int, set[str]]:
        nonlocal requests
        exits = set()
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(family=family, force_close=True, use_dns_cache=False),
            timeout=timeout,
            trust_env=False,
            headers={"User-Agent": USER_AGENT, "Connection": "close"},
        ) as session:
            for _ in range(cfg["baseline_rounds"]):
                requests += 1
                try:
                    async with session.get(url, allow_redirects=False) as response:
                        try:
                            body = await response.content.readexactly(MAX_TRACE_BYTES + 1)
                        except asyncio.IncompleteReadError as exc:
                            body = exc.partial
                        if (
                            response.status != 200
                            or "cloudflare" not in response.headers.get("Server", "").lower()
                        ):
                            continue
                        trace, reason = parse_trace(
                            body,
                            parts.hostname,
                            parts.port or (443 if parts.scheme == "https" else 80),
                            parts.scheme,
                            USER_AGENT,
                            colo_from_headers(response.headers),
                        )
                        if reason == "ok" and trace:
                            ip = normalize_ip(trace.exit_ip)
                            if ipaddress.ip_address(ip).version == version:
                                exits.add(ip)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    continue
        return version, exits

    baselines = dict(await asyncio.gather(collect(4, socket.AF_INET), collect(6, socket.AF_INET6)))
    context = ProxyContext(baselines, requests)
    if not context.baselines:
        raise OptimizerError("反代验证无法取得直连出口基准，请检查 proxyip.baseline_url 和本机直连网络")
    LOG.info("反代验证：直连基准可用地址族 %s；仅与相同地址族比较", context.stats["baseline_families"])
    return context


def filter_type(node: Node, allowed: list[str]) -> str:
    node.proxy_exit_type = exit_type(node.proxy_exit_ips)
    if not node.proxy_exit_type:
        return "反代验证未提供有效出口地址"
    if allowed and node.proxy_exit_type not in allowed:
        return f"出口类型 {node.proxy_exit_type} 不在 proxyip.exit_types 允许列表中"
    return "ok"
