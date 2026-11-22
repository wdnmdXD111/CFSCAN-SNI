from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path

import aiohttp

from .models import Node
from .proxyip import normalize_ip
from .util import OptimizerError, bounded_map, read_tokens, resolve_path, write_json

LOG = logging.getLogger(__name__)
API_URL = "https://api.ipapi.is/"
LEVELS = ("极度纯净", "纯净", "轻微风险", "高风险", "极度危险")
CACHE_VERSION = 1


def risk_level(score: float) -> str:
    for threshold, level in ((100, LEVELS[4]), (20, LEVELS[3]), (5, LEVELS[2]), (0.25, LEVELS[1])):
        if score >= threshold:
            return level
    return LEVELS[0]


def unknown(reason: str) -> dict:
    return {"level": "未知", "score": None, "reason": reason}


def parse_score(value: object) -> float:
    if isinstance(value, str):
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s*(?:\([^)]*\))?\s*", value)
        if not match:
            raise ValueError("invalid score")
        value = float(match.group(1))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing score")
    if not 0 <= value <= 1:
        raise ValueError("invalid score")
    return float(value)


def parse_risk(payload: object, ip: str) -> dict:
    """Use cfnb's formula, but never turn missing fields into a clean verdict."""
    if not isinstance(payload, dict) or "error" in payload or "error_code" in payload:
        return unknown("风险 API 返回错误或无效 JSON 对象")
    try:
        if normalize_ip(payload.get("ip")) != normalize_ip(ip):
            return unknown("风险 API 返回的 IP 与查询出口不一致")
        flags = {}
        for key in ("is_bogon", "is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser"):
            value = payload.get(key)
            if key == "is_crawler" and isinstance(value, str) and value.strip():
                value = True
            if not isinstance(value, bool):
                return unknown(f"风险 API 缺少有效字段 {key}")
            flags[key] = value
        if flags["is_bogon"]:
            score = 100.0
        else:
            company = parse_score(payload["company"]["abuser_score"])
            asn = parse_score(payload["asn"]["abuser_score"])
            score = (
                (company + asn) / 2 * 5 + sum(flags[key] for key in flags if key != "is_bogon") * 0.15
            ) * 100
    except (KeyError, TypeError, ValueError, OverflowError):
        return unknown("风险 API 缺少有效的 company/asn 滥用评分或出口 IP")
    return {"level": risk_level(score), "score": score, "reason": "ok"}


class RiskFilter:
    def __init__(self, cfg: dict, base: Path):
        self.cfg = cfg
        self.base = base
        self.path = resolve_path(base, cfg["cache_file"])
        self.cache_enabled = cfg["cache_enabled"] and cfg["cache_ttl_hours"] > 0
        self.entries: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.semaphore = asyncio.Semaphore(cfg["concurrency"])
        self.session: aiohttp.ClientSession | None = None
        self.key: str | None = None
        self.blocked_reason = ""
        self.dirty = False
        self.stats = {
            "enabled": True,
            "cache_hits": 0,
            "unique_queried": 0,
            "requests": 0,
            "unknown": 0,
            "write_errors": 0,
        }
        if self.cache_enabled:
            self._load()

    def _valid(self, entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        stamp, score = entry.get("updated_at"), entry.get("score")
        try:
            return (
                all(
                    isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    for value in (stamp, score)
                )
                and 0 <= time.time() - stamp < self.cfg["cache_ttl_hours"] * 3600
                and score >= 0
            )
        except OverflowError:
            return False

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError) as exc:
            LOG.warning("IP 风险缓存无法读取，将重新查询 (%s)", type(exc).__name__)
            return
        if (
            not isinstance(raw, dict)
            or raw.get("version") != CACHE_VERSION
            or raw.get("provider") != "ipapi.is"
        ):
            return
        if not isinstance(raw.get("entries"), dict):
            return
        for ip, entry in raw["entries"].items():
            try:
                if normalize_ip(ip) == ip and self._valid(entry):
                    self.entries[ip] = entry
            except ValueError:
                continue

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None:
            keys = read_tokens(resolve_path(self.base, self.cfg["key_file"]))
            if len(keys) != 1:
                raise OptimizerError(
                    f"风险过滤需要 ipapi.is 密钥，请在 {self.cfg['key_file']} 中填写一行密钥"
                )
            self.key = keys[0]
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.cfg["timeout_seconds"]), trust_env=False
            )
        return self.session

    async def _fetch(self, ip: str) -> dict:
        session = self._get_session()
        for attempt in range(self.cfg["retries"] + 1):
            if self.blocked_reason:
                return unknown(self.blocked_reason)
            self.stats["requests"] += 1
            try:
                # POST keeps the API key out of request URLs and exception URLs.
                async with session.post(
                    API_URL, json={"q": ip, "key": self.key}, allow_redirects=False
                ) as response:
                    if response.status in (401, 403, 429):
                        self.blocked_reason = f"风险 API HTTP {response.status}，本轮停止新查询"
                        LOG.warning(self.blocked_reason)
                        return unknown(self.blocked_reason)
                    if response.status == 200:
                        try:
                            body = await response.content.readexactly(65537)
                        except asyncio.IncompleteReadError as exc:
                            body = exc.partial
                        if len(body) > 65536:
                            return unknown("风险 API 响应超过 64 KiB")
                        try:
                            return parse_risk(json.loads(body), ip)
                        except (ValueError, UnicodeError):
                            return unknown("风险 API 返回无效 JSON")
                    reason = f"风险 API HTTP {response.status}"
                    if response.status not in (500, 502, 503, 504):
                        return unknown(reason)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                reason = f"风险 API {type(exc).__name__}"
            if attempt < self.cfg["retries"]:
                await asyncio.sleep(min(2**attempt, 4))
        return unknown(reason)

    async def lookup(self, ip: str) -> dict:
        ip = normalize_ip(ip)
        async with self.locks.setdefault(ip, asyncio.Lock()):
            if ip in self.results:
                return self.results[ip]
            entry = self.entries.get(ip)
            if self.cache_enabled and self._valid(entry):
                result = {"level": risk_level(entry["score"]), "score": entry["score"], "reason": "ok"}
                self.stats["cache_hits"] += 1
            else:
                async with self.semaphore:
                    self.stats["unique_queried"] += 1
                    result = await self._fetch(ip)
                if result["level"] == "未知":
                    self.stats["unknown"] += 1
                elif self.cache_enabled:
                    self.entries[ip] = {"score": result["score"], "updated_at": time.time()}
                    self.dirty = True
            self.results[ip] = result
            return result

    async def filter(self, node: Node) -> str:
        if not node.proxy_exit_ips:
            return "风险过滤缺少反代出口 IP"
        node.proxy_risks = {ip: await self.lookup(ip) for ip in sorted(set(node.proxy_exit_ips))}
        for result in node.proxy_risks.values():
            level = result["level"]
            if level == "未知":
                if self.cfg["unknown_policy"] == "reject":
                    return f"出口风险未知：{result['reason']}"
            elif LEVELS.index(level) > LEVELS.index(self.cfg["max_level"]):
                return f"出口风险 {level} 超过允许上限 {self.cfg['max_level']}"
        return "ok"

    async def filter_all(self, nodes: list[Node]) -> tuple[list[Node], dict[str, str]]:
        reasons = await bounded_map(self.filter, nodes, self.cfg["concurrency"])
        passed = [node for node, reason in zip(nodes, reasons) if reason == "ok"]
        rejected = {node.endpoint.address: reason for node, reason in zip(nodes, reasons) if reason != "ok"}
        self.save()
        LOG.info(
            "出口 IP 风险过滤：通过 %d/%d，缓存 %d，查询出口 %d，未知 %d",
            len(passed),
            len(nodes),
            self.stats["cache_hits"],
            self.stats["unique_queried"],
            self.stats["unknown"],
        )
        return passed, rejected

    def save(self) -> None:
        if not self.cache_enabled or not self.dirty:
            return
        try:
            write_json(
                self.path,
                {
                    "version": CACHE_VERSION,
                    "provider": "ipapi.is",
                    "entries": {ip: entry for ip, entry in self.entries.items() if self._valid(entry)},
                },
            )
            self.dirty = False
        except OSError as exc:
            self.stats["write_errors"] += 1
            LOG.warning("IP 风险缓存保存失败 (%s)", type(exc).__name__)

    async def close(self) -> None:
        try:
            if self.session is not None:
                await self.session.close()
        finally:
            self.save()
