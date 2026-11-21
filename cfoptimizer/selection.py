from __future__ import annotations

import logging
import random
from collections import Counter, defaultdict

from .models import Endpoint, Node
from .util import OptimizerError

LOG = logging.getLogger(__name__)


def prepare(endpoints: list[Endpoint], cfg: dict) -> tuple[list[Endpoint], dict]:
    """Filter endpoint ports before any IPinfo request; optionally sample globally."""
    ports = set(cfg["ports"])
    filtered = [endpoint for endpoint in endpoints if not ports or endpoint.port in ports]
    report = {
        "source_candidates": len(endpoints),
        "ports": cfg["ports"],
        "after_port_filter": len(filtered),
        "sample_size": cfg["sample_size"],
        "sample_scope": cfg["sample_scope"],
        "ports_available": dict(sorted(Counter(str(endpoint.port) for endpoint in filtered).items())),
    }
    LOG.info("候选端口过滤：%d → %d，允许端口=%s", len(endpoints), len(filtered), sorted(ports) or "全部")
    if not filtered:
        raise OptimizerError("端口过滤后没有候选；请检查 candidates.ports 和源文件中的实际端口")
    size = int(cfg["sample_size"])
    if size and cfg["sample_scope"] == "global":
        filtered = random.sample(filtered, min(size, len(filtered)))
        LOG.info("全局随机抽样：选取 %d 条 IP:端口，之后才进行 Lite 校准和 Zone 验证", len(filtered))
    report["before_ipinfo"] = len(filtered)
    return filtered, report


def sample_regions(nodes: list[Node], cfg: dict) -> tuple[list[Node], dict]:
    """Sample within calibrated IP countries, never guessing from source labels."""
    if not cfg["sample_size"] or cfg["sample_scope"] != "per_region":
        return nodes, {}
    grouped = defaultdict(list)
    for node in nodes:
        grouped[node.geo.country_code].append(node)
    selected = []
    counts = {}
    for code, regional in grouped.items():
        sampled = random.sample(regional, min(int(cfg["sample_size"]), len(regional)))
        selected.extend(sampled)
        counts[code] = {"available": len(regional), "sampled": len(sampled)}
        LOG.info("%s 随机抽样：%d → %d 条 IP:端口", code, len(regional), len(sampled))
    return selected, counts
