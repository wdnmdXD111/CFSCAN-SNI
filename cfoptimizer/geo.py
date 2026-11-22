from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path

import aiohttp
from babel import Locale

from .models import Endpoint, Geo
from .util import OptimizerError, bounded_map, read_tokens, resolve_path, write_json

LOG = logging.getLogger(__name__)
COUNTRIES_ZH = Locale.parse("zh_Hans").territories
NAME_OVERRIDES = {"HK": "中国香港", "MO": "中国澳门", "TW": "中国台湾"}


def country_name(code: str) -> str:
    return NAME_OVERRIDES.get(code) or COUNTRIES_ZH.get(code, "未知国家")


def parse_lite(payload: object, now: float | None = None) -> Geo | None:
    if not isinstance(payload, dict) or payload.get("bogon"):
        return None
    code = str(payload.get("country_code", "")).upper()
    if not re.fullmatch(r"[A-Z]{2}", code) or code not in COUNTRIES_ZH:
        return None
    # Lite.country is a full English name; never treat it as the old API's code.
    return Geo(code, country_name(code), now if now is not None else time.time())


def read_cache(path: Path, ttl_hours: float) -> dict[str, Geo]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
    except (ValueError, OSError):
        return {}
    now = time.time()
    result = {}
    for ip, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            stamp = float(data["updated_at"])
            if not math.isfinite(stamp) or not 0 <= now - stamp <= ttl_hours * 3600:
                continue
            geo = parse_lite(data, stamp)
            if geo:
                result[ip] = geo
        except (KeyError, TypeError, ValueError):
            continue
    return result


def tokens_from_config(cfg: dict, base: Path) -> list[str]:
    return read_tokens(resolve_path(base, cfg["token_file"]))


class LiteClient:
    def __init__(self, session, cfg: dict, tokens: list[str]):
        self.session = session
        self.cfg = cfg
        self.tokens = tokens
        self.disabled: set[str] = set()
        self.index = 0
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0

    async def lookup(self, ip: str) -> Geo | None:
        # Invalid-token rotation does not consume the transient retry allowance.
        attempts = 0
        while attempts <= int(self.cfg["retries"]):
            available = [token for token in self.tokens if token not in self.disabled]
            if not available:
                return None
            token = available[self.index % len(available)]
            self.index += 1
            async with self.rate_lock:
                wait = self.next_request - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.next_request = time.monotonic() + float(self.cfg["min_interval_seconds"])
            try:
                async with self.session.get(
                    f"{self.cfg['api_base'].rstrip('/')}/{ip}",
                    params={"token": token},
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        self.disabled.add(token)
                        continue
                    if response.status == 200:
                        raw = await response.content.read(65537)
                        if len(raw) > 65536:
                            return None
                        try:
                            return parse_lite(json.loads(raw))
                        except (ValueError, UnicodeError):
                            return None
                    if response.status not in (429, 500, 502, 503, 504):
                        return None
                    try:
                        wait_seconds = min(10.0, max(0.1, float(response.headers.get("Retry-After", 1))))
                    except ValueError:
                        wait_seconds = 1.0
            except (aiohttp.ClientError, asyncio.TimeoutError):
                wait_seconds = min(2**attempts, 4)
            attempts += 1
            if attempts <= int(self.cfg["retries"]):
                await asyncio.sleep(wait_seconds)
        return None


async def calibrate(endpoints: list[Endpoint], cfg: dict, base: Path) -> dict[str, Geo]:
    path = resolve_path(base, cfg["cache_file"])
    cache = read_cache(path, cfg["cache_ttl_hours"])
    ips = list(dict.fromkeys(endpoint.ip for endpoint in endpoints))
    missing = [ip for ip in ips if ip not in cache]
    if missing:
        tokens = tokens_from_config(cfg, base)
        if not tokens:
            raise OptimizerError(
                f"有 {len(missing)} 个 IP 未校准，请在 {cfg['token_file']} 中填写 IPinfo Lite Token（一行一个）"
            )
        timeout = aiohttp.ClientTimeout(total=float(cfg["timeout_seconds"]))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=bool(cfg["trust_env"])) as session:
            client = LiteClient(session, cfg, tokens)
            done = 0
            last_log = time.monotonic()

            async def lookup(ip):
                nonlocal done, last_log
                value = await client.lookup(ip)
                done += 1
                if done == len(missing) or time.monotonic() - last_log > 3:
                    LOG.info("Lite 地区校准 %d/%d", done, len(missing))
                    last_log = time.monotonic()
                return value

            values = await bounded_map(lookup, missing, cfg["concurrency"])
        for ip, value in zip(missing, values):
            if value:
                cache[ip] = value
        write_json(
            path,
            {
                ip: {"country_code": value.country_code, "updated_at": value.updated_at}
                for ip, value in cache.items()
            },
        )
    result = {ip: cache[ip] for ip in ips if ip in cache}
    failures = len(ips) - len(result)
    LOG.info("Lite 校准：%d 个 IP，缓存 %d，失败 %d", len(ips), len(ips) - len(missing), failures)
    if failures:
        LOG.warning("%d 个未完成校准的 IP 将跳过，不使用原标签冒充校准成功", failures)
    if not result or (failures and cfg["require_all"]):
        raise OptimizerError("Lite 校准失败，请检查 Token、限流和网络；未写入新的 ip.txt")
    return result
