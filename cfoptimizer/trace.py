from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

TRACE_PATH = "/cdn-cgi/trace"
MAX_TRACE_BYTES = 65536
USER_AGENT = "CFSCAN-SNI/1.0"


def is_trace_path(path: str) -> bool:
    return urlsplit(path).path == TRACE_PATH


@dataclass(frozen=True)
class TraceResult:
    colo: str
    exit_ip: str


def parse_trace(
    body: bytes, domain: str, port: int, scheme: str, user_agent: str, header_colo: str
) -> tuple[TraceResult | None, str]:
    """Return only verified fields; never retain the raw trace."""
    if len(body) > MAX_TRACE_BYTES:
        return None, "trace 正文超过 64 KiB"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, "trace 正文不是有效 UTF-8"
    fields = {}
    for line in text.splitlines():
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) or key in fields:
            return None, "trace 字段格式无效或重复"
        fields[key] = value
    for key in ("h", "ip", "colo", "visit_scheme", "uag", "tls"):
        if not fields.get(key):
            return None, f"trace 缺少有效字段 {key}"
    try:
        host = fields["h"]
        authority = urlsplit(f"//{host}")
        if (
            any(char.isspace() for char in host)
            or authority.netloc != host
            or host.endswith(":")
            or authority.username is not None
            or authority.password is not None
            or authority.path
            or authority.query
            or authority.fragment
            or authority.port not in (None, port)
            or (authority.hostname or "").lower().rstrip(".") != domain.lower().rstrip(".")
        ):
            return None, "trace h 与验证域名或端口不一致"
    except ValueError:
        return None, "trace h 不是有效主机名"
    if fields["uag"] != user_agent:
        return None, "trace uag 与本次请求的 User-Agent 不一致"
    if fields["visit_scheme"] != scheme:
        return None, "trace visit_scheme 与请求协议不一致"
    tls = fields["tls"]
    if (scheme == "https" and not re.fullmatch(r"TLSv[0-9]+(?:\.[0-9]+)*", tls)) or (
        scheme == "http" and tls != "off"
    ):
        return None, "trace tls 与请求协议不一致"
    try:
        ipaddress.ip_address(fields["ip"])
        if "%" in fields["ip"]:
            raise ValueError("scoped address")
    except ValueError:
        return None, "trace ip 不是有效 IPv4/IPv6 地址"
    colo = fields["colo"]
    if not re.fullmatch(r"[A-Z0-9]{3,5}", colo):
        return None, "trace colo 不是有效机房代码"
    if not header_colo or colo != header_colo:
        return None, "trace colo 与 CF-Ray 机房不一致"
    return TraceResult(colo, str(ipaddress.ip_address(fields["ip"]))), "ok"


def validate_trace(
    body: bytes, domain: str, port: int, scheme: str, user_agent: str, header_colo: str
) -> tuple[str, str]:
    result, reason = parse_trace(body, domain, port, scheme, user_agent, header_colo)
    return (result.colo if result else ""), reason
