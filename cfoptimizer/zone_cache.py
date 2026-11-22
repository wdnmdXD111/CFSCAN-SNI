from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path

from .models import Node
from .proxyip import ProxyContext, exit_type, normalize_ip
from .util import resolve_path, write_json

LOG = logging.getLogger(__name__)
VERSION = 3
RULES = (
    "domain",
    "path",
    "scheme",
    "verify_tls",
    "timeout_seconds",
    "connect_timeout_seconds",
    "rounds",
    "accepted_statuses",
    "require_cf_headers",
    "expected_body",
    "allowed_colos",
)


def fingerprint(cfg: dict, proxy: ProxyContext | None = None) -> str:
    rules = {key: cfg[key] for key in RULES}
    rules["domain"] = str(rules["domain"]).lower().rstrip(".")
    rules["accepted_statuses"] = sorted(set(rules["accepted_statuses"]))
    rules["allowed_colos"] = sorted(set(rules["allowed_colos"]))
    rules["proxyip"] = proxy.fingerprint if proxy is not None else None
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


def finite_number(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


class ZoneCache:
    """Persist endpoint results scoped to the complete set of verification rules."""

    def __init__(self, cfg: dict, base: Path, proxy: ProxyContext | None = None):
        self.cfg = cfg
        self.path = resolve_path(base, cfg["cache_file"])
        self.proxy = proxy
        self.fingerprint = fingerprint(cfg, proxy)
        self.enabled = cfg["cache_enabled"] and cfg["cache_ttl_hours"] > 0
        self.entries: dict[str, dict] = {}
        self.reused: set[str] = set()
        self.dirty = False
        self.last_save = time.monotonic()
        self.stats = {
            "enabled": self.enabled,
            "file": str(self.path),
            "hit_passed": 0,
            "hit_rejected": 0,
            "fresh": 0,
            "rechecked": 0,
            "recheck_failed": 0,
            "write_errors": 0,
        }
        if self.enabled:
            self._load()

    def _valid(self, entry: object, now: float) -> bool:
        if not isinstance(entry, dict) or not isinstance(entry.get("passed"), bool):
            return False
        ttl = self.cfg["cache_ttl_hours"] if entry["passed"] else self.cfg["cache_failure_ttl_hours"]
        stamp = entry.get("updated_at")
        if not ttl or not finite_number(stamp) or not 0 <= now - stamp < ttl * 3600:
            return False
        if not isinstance(entry.get("reason"), str) or not entry["reason"]:
            return False
        if not entry["passed"]:
            return True
        status, colo = entry.get("status"), entry.get("colo")
        if self.proxy is not None and self.proxy.check_exits(entry.get("exit_ips")) != "ok":
            return False
        return (
            isinstance(status, int)
            and not isinstance(status, bool)
            and status in self.cfg["accepted_statuses"]
            and isinstance(colo, str)
            and (colo == "" or re.fullmatch(r"[A-Z0-9]{3,5}", colo) is not None)
            and (not self.cfg["require_cf_headers"] or bool(colo))
            and (not self.cfg["allowed_colos"] or colo in self.cfg["allowed_colos"])
            and all(finite_number(entry.get(key)) and entry[key] >= 0 for key in ("latency_ms", "jitter_ms"))
        )

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError) as exc:
            LOG.warning("Zone 缓存无法读取，将重新验证 (%s)", type(exc).__name__)
            return
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] != VERSION
            or raw.get("fingerprint") != self.fingerprint
            or not isinstance(raw.get("entries"), dict)
        ):
            LOG.info("Zone 缓存版本或验证配置不匹配，将重新验证")
            return
        now = time.time()
        self.entries = {
            address: entry for address, entry in raw["entries"].items() if self._valid(entry, now)
        }
        self.dirty = len(self.entries) != len(raw["entries"])

    def lookup(self, node: Node) -> tuple[Node | None, str] | None:
        if not self.enabled:
            return None
        address = node.endpoint.address
        entry = self.entries.get(address)
        if entry is None:
            return None
        if not self._valid(entry, time.time()):
            self.entries.pop(address, None)
            self.dirty = True
            return None
        if not entry["passed"]:
            self.stats["hit_rejected"] += 1
            return None, entry["reason"]
        for key in ("colo", "status", "latency_ms", "jitter_ms"):
            setattr(node, key, entry[key])
        if self.proxy is not None:
            node.proxy_exit_ips = sorted({normalize_ip(ip) for ip in entry["exit_ips"]})
            node.proxy_exit_type = exit_type(node.proxy_exit_ips)
            node.proxy_risks = {}
        self.reused.add(address)
        self.stats["hit_passed"] += 1
        return node, entry["reason"]

    def record(self, node: Node, result: Node | None, reason: str) -> None:
        address = node.endpoint.address
        self.reused.discard(address)
        if not self.enabled:
            return
        entry = {"passed": result is not None, "reason": reason, "updated_at": time.time()}
        if result is not None:
            entry.update({key: getattr(result, key) for key in ("colo", "status", "latency_ms", "jitter_ms")})
            if self.proxy is not None:
                entry["exit_ips"] = list(result.proxy_exit_ips)
        if self._valid(entry, time.time()):
            self.entries[address] = entry
        else:
            self.entries.pop(address, None)
        self.dirty = True

    def save(self, force: bool = False) -> None:
        if not self.enabled or not self.dirty or (not force and time.monotonic() - self.last_save < 5):
            return
        self.last_save = time.monotonic()
        now = time.time()
        self.entries = {address: entry for address, entry in self.entries.items() if self._valid(entry, now)}
        try:
            write_json(
                self.path, {"version": VERSION, "fingerprint": self.fingerprint, "entries": self.entries}
            )
        except OSError as exc:
            self.stats["write_errors"] += 1
            LOG.warning("Zone 缓存保存失败，当前验证结果仍有效 (%s)", type(exc).__name__)
            return
        self.dirty = False
