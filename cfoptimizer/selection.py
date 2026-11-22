from __future__ import annotations

import logging
from collections import Counter, defaultdict

from .models import Endpoint, Node
from .util import OptimizerError

LOG = logging.getLogger(__name__)


def prepare(endpoints: list[Endpoint], cfg: dict) -> tuple[list[Endpoint], dict]:
    """Filter endpoint ports before any IPinfo request."""
    ports = set(cfg["ports"])
    filtered = [endpoint for endpoint in endpoints if not ports or endpoint.port in ports]
    report = {
        "source_candidates": len(endpoints),
        "ports": cfg["ports"],
        "after_port_filter": len(filtered),
        "sample_size": cfg["sample_size"],
        "sample_scope": cfg["sample_scope"],
        "selection_method": "tcp_latency",
        "ports_available": dict(sorted(Counter(str(endpoint.port) for endpoint in filtered).items())),
    }
    LOG.info("候选端口过滤：%d → %d，允许端口=%s", len(endpoints), len(filtered), sorted(ports) or "全部")
    if not filtered:
        raise OptimizerError("端口过滤后没有候选；请检查 candidates.ports 和源文件中的实际端口")
    report["before_ipinfo"] = len(filtered)
    return filtered, report


def shortlist_tcp(nodes: list[Node], cfg: dict) -> tuple[list[Node], dict]:
    """Take TCP-passed endpoints by ascending latency, globally or per Lite country."""
    ranked = sorted(nodes, key=lambda node: (node.tcp_latency_ms, node.endpoint))
    size = int(cfg["sample_size"])
    if not size:
        LOG.info("TCP 延迟筛选：不限制数量，全部 %d 个通过候选进入 Zone", len(ranked))
        return ranked, {}
    if cfg["sample_scope"] == "global":
        selected = ranked[:size]
        LOG.info("全局 TCP 延迟前 N 筛选：%d → %d 条 IP:端口，之后进入 Zone", len(nodes), len(selected))
        return selected, {}
    grouped = defaultdict(list)
    for node in ranked:
        grouped[node.geo.country_code].append(node)
    selected = []
    counts = {}
    for code in sorted(grouped):
        regional = grouped[code]
        shortlisted = regional[:size]
        selected.extend(shortlisted)
        counts[code] = {"available": len(regional), "sampled": len(shortlisted)}
        LOG.info("%s TCP 延迟前 N 筛选：%d → %d 条 IP:端口", code, len(regional), len(shortlisted))
    selected.sort(key=lambda node: (node.tcp_latency_ms, node.endpoint))
    return selected, counts
