from __future__ import annotations

import copy
import json
import ipaddress
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from babel import Locale

from .util import OptimizerError


DEFAULTS = {
    "zone": {
        "domain": "",
        "path": "/",
        "scheme": "https",
        "verify_tls": True,
        "timeout_seconds": 5,
        "connect_timeout_seconds": 2,
        "concurrency": 48,
        "rounds": 2,
        "accepted_statuses": [200, 204, 301, 302, 307, 308, 404],
        "require_cf_headers": True,
        "expected_body": "",
        "allowed_colos": [],
        "cache_file": ".state/zone_cache.json",
        "cache_ttl_hours": 6,
        "cache_failure_ttl_hours": 0.25,
    },
    "candidates": {"ports": [], "sample_size": 0, "sample_scope": "global"},
    "sources": [],
    "source_timeout_seconds": 20,
    "source_max_bytes": 8388608,
    "source_max_candidates": 50000,
    "default_port": 443,
    "cidr_samples": 64,
    "regions": {"HK": 3, "JP": 3, "SG": 3, "US": 3},
    "region_basis": "ipinfo",
    "colo_countries": {
        "HKG": "HK",
        "NRT": "JP",
        "KIX": "JP",
        "FUK": "JP",
        "OKA": "JP",
        "SIN": "SG",
        "TPE": "TW",
        "KHH": "TW",
        "ICN": "KR",
        "MFM": "MO",
        "LAX": "US",
        "SJC": "US",
        "SFO": "US",
        "SEA": "US",
        "PDX": "US",
        "ORD": "US",
        "DFW": "US",
        "IAD": "US",
        "EWR": "US",
        "BOS": "US",
        "ATL": "US",
        "MIA": "US",
        "DEN": "US",
        "PHX": "US",
        "YYZ": "CA",
        "YVR": "CA",
        "LHR": "GB",
        "MAN": "GB",
        "FRA": "DE",
        "HAM": "DE",
        "AMS": "NL",
        "CDG": "FR",
        "MAD": "ES",
        "MXP": "IT",
        "ARN": "SE",
        "ZRH": "CH",
        "WAW": "PL",
        "VIE": "AT",
        "HEL": "FI",
        "LIS": "PT",
        "SYD": "AU",
        "MEL": "AU",
        "BNE": "AU",
        "PER": "AU",
        "AKL": "NZ",
        "BKK": "TH",
        "KUL": "MY",
        "MNL": "PH",
        "CEB": "PH",
        "CGK": "ID",
        "BOM": "IN",
        "DEL": "IN",
        "MAA": "IN",
        "BLR": "IN",
        "DXB": "AE",
        "DUB": "IE",
        "JNB": "ZA",
        "GRU": "BR",
        "SCL": "CL",
        "EZE": "AR",
    },
    "ipinfo": {
        "api_base": "https://api.ipinfo.io/lite",
        "token_file": "ipinfo_tokens.txt",
        "cache_file": ".state/ipinfo_lite.json",
        "cache_ttl_hours": 168,
        "concurrency": 8,
        "timeout_seconds": 6,
        "retries": 2,
        "min_interval_seconds": 0.1,
        "require_all": False,
        "trust_env": True,
    },
    "speed": {
        "mode": "static",
        "url": "https://speed.cloudflare.com/__down?bytes=100000000",
        "duration_seconds": 5,
        "max_bytes": 20000000,
        "streams": 4,
        "repeats": 3,
        "timeout_seconds": 8,
        "min_bytes": 65536,
        "candidates_per_region": 12,
        "min_mbps": 0,
        "max_latency_ms": 0,
        "sort_by": "speed",
        "token_file": "speed_token.txt",
    },
    "output": {"file": "ip.txt", "report_file": "report.json", "include_metrics": True},
    "github": {
        "enabled": False,
        "remote": "origin",
        "branch": "",
        "retries": 3,
        "retry_delay_seconds": 3,
        "timeout_seconds": 45,
        "commit_message": "chore: update regional Cloudflare IP list",
        "status_file": ".state/github_sync.json",
    },
}


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key == "regions":
            result[key] = copy.deepcopy(value)
        else:
            result[key] = (
                merge(result[key], value)
                if isinstance(value, dict) and isinstance(result.get(key), dict)
                else value
            )
    return result


def parse_regions(value: dict | str) -> dict[str, int]:
    try:
        if isinstance(value, str):
            entries = [part.strip().split(":") for part in value.split(",") if part.strip()]
            if any(len(pair) != 2 for pair in entries):
                raise ValueError("使用 HK:3,JP:2 这样的格式")
            value = dict(entries)
        if not isinstance(value, dict) or not value:
            raise ValueError("至少指定一个国家/地区，或用 * 指定所有地区")
        result = {}
        territories = Locale.parse("zh_Hans").territories
        for key, count in value.items():
            code = str(key).strip().upper()
            if code != "*" and (not re.fullmatch(r"[A-Z]{2}", code) or code not in territories):
                raise ValueError(f"未知国家/地区代码: {key}")
            if isinstance(count, bool) or str(count).strip() != str(int(count)) or int(count) <= 0:
                raise ValueError(f"{code} 的配额必须是正整数")
            result[code] = int(count)
        if "*" in result and len(result) != 1:
            raise ValueError("* 不能与其他地区混用")
        return result
    except (ValueError, TypeError) as exc:
        raise OptimizerError(f"regions 配置错误: {exc}") from exc


def validate(cfg: dict) -> dict:
    zone = cfg["zone"]
    domain = str(zone["domain"]).strip().rstrip(".")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OptimizerError("zone.domain 不是有效域名") from exc
    if not domain or len(domain) > 253 or any(c in domain for c in "/:@?# \\\t\r\n"):
        raise OptimizerError("请填写 zone.domain，只填自己的 CF 域名，不带 https://、端口或路径")
    if any(not re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)", label) for label in domain.split(".")):
        raise OptimizerError("zone.domain 不是有效域名")
    zone["domain"] = domain
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise OptimizerError("zone.domain 必须使用域名，以便验证 SNI/Host")
    if zone["scheme"] not in ("https", "http"):
        raise OptimizerError("zone.scheme 只能是 https 或 http")
    if not str(zone["path"]).startswith("/") or "\r" in zone["path"] or "\n" in zone["path"]:
        raise OptimizerError("zone.path 必须以 / 开头")
    cfg["regions"] = parse_regions(cfg["regions"])
    if cfg["region_basis"] not in ("ipinfo", "colo"):
        raise OptimizerError("region_basis 只能是 ipinfo 或 colo")
    if not isinstance(cfg["colo_countries"], dict) or any(
        not re.fullmatch(r"[A-Z0-9]{3,5}", key) or code not in Locale.parse("zh_Hans").territories
        for key, code in cfg["colo_countries"].items()
    ):
        raise OptimizerError("colo_countries 必须是机房码到国家代码的映射，例如 HKG: HK")
    if not isinstance(cfg["sources"], list) or not cfg["sources"]:
        raise OptimizerError("sources 必须包含至少一个本地文件路径或 HTTP(S) URL")
    candidates = cfg["candidates"]
    ports = candidates["ports"]
    if not isinstance(ports, list) or any(
        not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535 for port in ports
    ):
        raise OptimizerError("candidates.ports 必须是 1..65535 的整数列表，空列表表示不过滤端口")
    candidates["ports"] = list(dict.fromkeys(ports))
    size = candidates["sample_size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise OptimizerError("candidates.sample_size 必须是非负整数，0 表示全量")
    if candidates["sample_scope"] not in ("global", "per_region"):
        raise OptimizerError("candidates.sample_scope 只能是 global 或 per_region")
    if size and candidates["sample_scope"] == "per_region" and cfg["region_basis"] != "ipinfo":
        raise OptimizerError("per_region 抽样需要 region_basis=ipinfo；colo 分组请使用 global 抽样")
    if not isinstance(zone["cache_file"], str) or not zone["cache_file"].strip():
        raise OptimizerError("zone.cache_file 必须是非空文件路径")
    zone["cache_file"] = zone["cache_file"].strip()
    speed = cfg["speed"]
    if speed["mode"] not in ("static", "worker"):
        raise OptimizerError("speed.mode 只能是 static 或 worker")
    parsed = urlsplit(str(speed["url"]))
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise OptimizerError("speed.url 必须是有效 HTTP(S) 地址，不能含用户名、密码或 fragment")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise OptimizerError("speed.url 必须使用域名，不能使用 IP 字面量绕过候选解析")
    if speed["mode"] == "worker" and parsed.hostname.lower() != domain.lower():
        raise OptimizerError("worker 模式的 speed.url 必须使用 zone.domain，确保测速经过同一域名")
    for section, required in ((cfg["ipinfo"], True), (speed, False)):
        value = section["token_file"]
        if not isinstance(value, str) or (required and not value.strip()):
            raise OptimizerError("ipinfo.token_file 必须是非空路径，speed.token_file 可留空")
        section["token_file"] = value.strip()
    if not 1 <= int(cfg["default_port"]) <= 65535:
        raise OptimizerError("default_port 必须在 1..65535 之间")
    positive = [
        (zone, "timeout_seconds"),
        (zone, "connect_timeout_seconds"),
        (zone, "concurrency"),
        (zone, "rounds"),
        (speed, "duration_seconds"),
        (speed, "timeout_seconds"),
        (speed, "max_bytes"),
        (speed, "min_bytes"),
        (speed, "streams"),
        (speed, "repeats"),
        (cfg["ipinfo"], "concurrency"),
        (cfg["ipinfo"], "timeout_seconds"),
        (cfg["github"], "timeout_seconds"),
        (cfg["github"], "retries"),
        (cfg, "source_max_bytes"),
        (cfg, "source_max_candidates"),
        (cfg, "cidr_samples"),
        (cfg, "source_timeout_seconds"),
    ]
    for section, key in positive:
        try:
            if isinstance(section[key], bool):
                raise ValueError()
            value = float(section[key])
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise OptimizerError(f"{key} 必须是有限正数") from None
        if key not in (
            "concurrency",
            "rounds",
            "retries",
            "streams",
            "repeats",
            "min_bytes",
            "max_bytes",
            "source_max_bytes",
            "source_max_candidates",
            "cidr_samples",
        ):
            section[key] = value
    for section, key in [
        (speed, "candidates_per_region"),
        (speed, "min_mbps"),
        (speed, "max_latency_ms"),
        (zone, "cache_ttl_hours"),
        (zone, "cache_failure_ttl_hours"),
        (cfg["ipinfo"], "cache_ttl_hours"),
        (cfg["ipinfo"], "retries"),
        (cfg["ipinfo"], "min_interval_seconds"),
        (cfg["github"], "retry_delay_seconds"),
    ]:
        if isinstance(section[key], bool):
            raise OptimizerError(f"{key} 必须是有限非负数")
        value = float(section[key])
        if not math.isfinite(value) or value < 0:
            raise OptimizerError(f"{key} 必须是有限非负数")
        if key not in ("candidates_per_region", "retries"):
            section[key] = value
    if int(speed["streams"]) > 16:
        raise OptimizerError("speed.streams 不能超过 16")
    if int(speed["min_bytes"]) > int(speed["max_bytes"]):
        raise OptimizerError("speed.min_bytes 不能大于 max_bytes")
    if speed["sort_by"] not in ("speed", "latency"):
        raise OptimizerError("speed.sort_by 只能是 speed 或 latency")
    for section, keys in [
        (zone, ["concurrency", "rounds"]),
        (speed, ["streams", "repeats", "max_bytes", "min_bytes", "candidates_per_region"]),
        (cfg, ["default_port", "cidr_samples", "source_max_bytes", "source_max_candidates"]),
        (cfg["ipinfo"], ["concurrency", "retries"]),
        (cfg["github"], ["retries"]),
    ]:
        for key in keys:
            if isinstance(section[key], bool) or float(section[key]) != int(section[key]):
                raise OptimizerError(f"{key} 必须是整数")
            section[key] = int(section[key])
    for section, keys in [
        (zone, ["verify_tls", "require_cf_headers"]),
        (cfg["ipinfo"], ["require_all", "trust_env"]),
        (cfg["github"], ["enabled"]),
        (cfg["output"], ["include_metrics"]),
    ]:
        for key in keys:
            if not isinstance(section[key], bool):
                raise OptimizerError(f"{key} 必须是 JSON 布尔值 true/false")
    api = urlsplit(str(cfg["ipinfo"]["api_base"]))
    if api.scheme != "https" or not api.hostname or api.username or api.password or api.query or api.fragment:
        raise OptimizerError("ipinfo.api_base 必须使用 HTTPS，Token 只发送到该地址")
    if (
        not isinstance(zone["accepted_statuses"], list)
        or not zone["accepted_statuses"]
        or any(
            not isinstance(code, int) or isinstance(code, bool) or not 100 <= code <= 599
            for code in zone["accepted_statuses"]
        )
    ):
        raise OptimizerError("zone.accepted_statuses 必须是 HTTP 状态码整数列表")
    if not isinstance(zone["allowed_colos"], list) or any(
        not isinstance(colo, str) or not re.fullmatch(r"[A-Z0-9]{3,5}", colo)
        for colo in zone["allowed_colos"]
    ):
        raise OptimizerError("zone.allowed_colos 必须是大写机房码列表")
    if not isinstance(zone["expected_body"], str):
        raise OptimizerError("zone.expected_body 必须是字符串")
    for key in ("file", "report_file"):
        if not str(cfg["output"][key]).strip():
            raise OptimizerError(f"output.{key} 不能为空")
    return cfg


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise OptimizerError(f"未找到 {path}，先执行 python main.py init") from exc
    except (ValueError, OSError) as exc:
        raise OptimizerError(f"配置读取失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise OptimizerError("配置顶层必须是 JSON 对象")
    # Old configurations remain loadable, but environment-based tokens are gone.
    for section in ("ipinfo", "speed", "github"):
        if isinstance(raw.get(section), dict):
            raw[section].pop("token_env", None)
    try:
        cfg = validate(merge(DEFAULTS, raw))
        paths = [
            path.resolve(),
            (path.parent / cfg["output"]["file"]).resolve(),
            (path.parent / cfg["output"]["report_file"]).resolve(),
            (path.parent / cfg["ipinfo"]["cache_file"]).resolve(),
            (path.parent / cfg["zone"]["cache_file"]).resolve(),
            (path.parent / cfg["ipinfo"]["token_file"]).resolve(),
            (path.parent / cfg["github"]["status_file"]).resolve(),
            (path.parent / ".state" / "run.lock").resolve(),
        ]
        if cfg["speed"]["token_file"]:
            paths.append((path.parent / cfg["speed"]["token_file"]).resolve())
        if len(set(paths)) != len(paths):
            raise OptimizerError("配置、Token、缓存、输出和状态文件的路径不能重复")
        if any(".git" in target.parts for target in paths[1:]):
            raise OptimizerError("数据文件不能写入 .git 目录")
        return cfg
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise OptimizerError(f"配置字段格式有误: {exc}") from exc
