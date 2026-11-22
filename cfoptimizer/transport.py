from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .models import Endpoint


class PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": self.endpoint.ip,
                "port": self.endpoint.port,
                "family": socket.AF_INET6
                if ipaddress.ip_address(self.endpoint.ip).version == 6
                else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        pass


def connector(endpoint: Endpoint, verify_tls: bool, streams: int = 1) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        resolver=PinnedResolver(endpoint),
        ssl=True if verify_tls else False,
        family=socket.AF_UNSPEC,
        limit=streams,
        limit_per_host=streams,
        use_dns_cache=False,
        enable_cleanup_closed=True,
    )


def pinned_url(url: str, port: int) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit((parts.scheme, f"{authority}:{port}", parts.path or "/", parts.query, ""))


def cf_error(body: bytes) -> str:
    text = body.decode("utf-8", errors="ignore")
    # Covers plain text, Cloudflare's <span>1034</span>, JSON, and title forms.
    plain = re.sub(r"<[^>]*>", " ", text)
    if "resolved to an ip address that the owner" in plain.lower():
        return "CF 1034"
    match = re.search(r"\berror(?:\s+code)?\s*[:=\"'\s-]*\s*(1\d{3})\b", plain, re.I)
    return f"CF {match.group(1)}" if match else ""


def colo_from_headers(headers) -> str:
    match = re.search(r"-([A-Z0-9]{3,5})$", headers.get("CF-Ray", ""), re.I)
    return match.group(1).upper() if match else ""
