from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, order=True)
class Endpoint:
    ip: str
    port: int = 443

    def __post_init__(self) -> None:
        object.__setattr__(self, "ip", str(ipaddress.ip_address(self.ip)))
        if not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1..65535 之间")

    @property
    def address(self) -> str:
        return f"[{self.ip}]:{self.port}" if ":" in self.ip else f"{self.ip}:{self.port}"


@dataclass
class Geo:
    country_code: str
    country_name: str
    updated_at: float


@dataclass
class Node:
    endpoint: Endpoint
    geo: Geo
    colo: str = ""
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    status: int = 0
    speed_mbps: float = 0.0
    bytes_received: int = 0
    speed_samples: list[float] = field(default_factory=list)
    region_code: str = ""
    region_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
