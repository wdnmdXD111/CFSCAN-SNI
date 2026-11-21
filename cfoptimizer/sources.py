from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .models import Endpoint
from .util import OptimizerError, resolve_path

LOG = logging.getLogger(__name__)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2}|:\d{1,5})?(?![\w./:])")
BRACKET_IPV6 = re.compile(r"\[[0-9a-fA-F:]+\](?::\d{1,5})?")


def parse_endpoint(raw: str, default_port: int = 443) -> Endpoint | None:
    value = raw.split("#", 1)[0].strip().split()
    if not value:
        return None
    token = value[0].strip(",;\"'`")
    try:
        port = default_port
        if token.startswith("["):
            end = token.index("]")
            ip = token[1:end]
            rest = token[end + 1 :]
            if rest:
                if not rest.startswith(":"):
                    return None
                port = int(rest[1:])
        elif token.count(":") == 1 and "." in token:
            ip, port_text = token.rsplit(":", 1)
            port = int(port_text)
        else:
            ip = token
        if "%" in ip or "/" in ip:
            return None
        return Endpoint(ip, port)
    except (ValueError, IndexError):
        return None


def sample_network(raw: str, count: int, port: int) -> list[Endpoint]:
    """Stratified deterministic sampling without ever materializing a CIDR."""
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return []
    skip_first = network.num_addresses > 2
    skip_last = network.version == 4 and network.num_addresses > 2
    first = int(network.network_address) + int(skip_first)
    available = network.num_addresses - int(skip_first) - int(skip_last)
    take = min(count, available)
    seed = int.from_bytes(hashlib.sha256(str(network).encode()).digest(), "big")
    result = []
    for i in range(take):
        low, high = available * i // take, available * (i + 1) // take
        offset = low + seed % (high - low)
        result.append(Endpoint(str(network.network_address.__class__(first + offset)), port))
    return result


def parse_source_text(text: str, port: int = 443, cidr_samples: int = 64) -> set[Endpoint]:
    result: set[Endpoint] = set()

    def parse_line(line: str) -> None:
        line = line.strip()
        if not line or line.startswith(("#", ";", "//")):
            return
        line = line.split("#", 1)[0]
        candidate = line.strip().strip(",\"'")
        if "/" in candidate and not any(c in candidate for c in " \t{}"):
            network = sample_network(candidate, cidr_samples, port)
            if network:
                result.update(network)
                return
        direct = parse_endpoint(candidate, port)
        if direct:
            result.add(direct)
            return
        for match in list(BRACKET_IPV6.finditer(line)) + list(IPV4.finditer(line)):
            token = match.group(0)
            if "/" in token:
                result.update(sample_network(token, cidr_samples, port))
            else:
                endpoint = parse_endpoint(token, port)
                if endpoint:
                    result.add(endpoint)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            address = value.get("ip") or value.get("address") or value.get("server")
            if isinstance(address, str):
                try:
                    endpoint = parse_endpoint(address, int(value.get("port", port)))
                    if endpoint:
                        result.add(endpoint)
                except (ValueError, TypeError):
                    pass
            for key, child in value.items():
                if key not in ("ip", "address", "server", "port"):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            for line in value.splitlines():
                parse_line(line)

    try:
        walk(json.loads(text))
    except (ValueError, TypeError):
        for line in text.splitlines():
            parse_line(line)
    return result


async def collect(cfg: dict, base: Path) -> list[Endpoint]:
    endpoints: set[Endpoint] = set()
    async with aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=cfg["source_timeout_seconds"])
    ) as session:
        for source in cfg["sources"]:
            if isinstance(source, dict):
                if not source.get("enabled", True):
                    continue
                source = source.get("url") or source.get("file", "")
            if not isinstance(source, str) or not source.strip():
                raise OptimizerError("sources 项目必须是路径、URL 或包含 url/file 的对象")
            try:
                if urlsplit(source).scheme in ("http", "https"):
                    async with session.get(source) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > cfg["source_max_bytes"]:
                                raise OptimizerError("候选源超过 source_max_bytes 限制")
                        text = body.decode("utf-8-sig", errors="replace")
                else:
                    path = resolve_path(base, source)
                    if path.stat().st_size > cfg["source_max_bytes"]:
                        raise OptimizerError("本地候选源超过 source_max_bytes 限制")
                    text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig", errors="replace")
                found = parse_source_text(text, cfg["default_port"], int(cfg["cidr_samples"]))
                endpoints.update(found)
                LOG.info("候选源 %s：解析 %d，累计去重 %d", source, len(found), len(endpoints))
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError, OptimizerError) as exc:
                LOG.warning("候选源读取失败 %s (%s)", source, type(exc).__name__)
            if len(endpoints) > cfg["source_max_candidates"]:
                raise OptimizerError("候选数量超过 source_max_candidates，调整源或提高上限；不会静默截断地区")
    if not endpoints:
        raise OptimizerError("没有解析到候选 IP，请检查 sources")
    return sorted(
        endpoints,
        key=lambda item: (
            ipaddress.ip_address(item.ip).version,
            int(ipaddress.ip_address(item.ip)),
            item.port,
        ),
    )
