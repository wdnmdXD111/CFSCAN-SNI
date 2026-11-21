from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import geo, probe, selection, sources, speed
from .models import Node
from .publish import publish
from .util import OptimizerError, atomic_write, resolve_path, write_json

LOG = logging.getLogger(__name__)


def format_node(node: Node, metrics: bool = True) -> str:
    code = node.region_code or node.geo.country_code
    name = node.region_name or node.geo.country_name
    line = f"{node.endpoint.address}#{code} {name}"
    if metrics:
        line += f" {node.latency_ms:.0f}ms {node.speed_mbps:.2f}Mbps"
        if node.colo:
            line += f" colo={node.colo}"
    return line


async def run(cfg: dict, base: Path, publish_result: bool = True) -> int:
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "success": False,
        "output_updated": False,
        "region_basis": cfg["region_basis"],
        "regions": {},
        "rejected": {},
        "selected": [],
        "github": {"state": "not_attempted", "success": False},
    }
    report_path = resolve_path(base, cfg["output"]["report_file"])
    zone_cache = None
    try:
        endpoints = await sources.collect(cfg, base)
        endpoints, report["candidate_pool"] = selection.prepare(endpoints, cfg["candidates"])
        locations = await geo.calibrate(endpoints, cfg["ipinfo"], base)
        report["rejected"].update(
            {
                endpoint.address: {"stage": "calibration", "reason": "Lite 未能校准该 IP 的国家"}
                for endpoint in endpoints
                if endpoint.ip not in locations
            }
        )
        nodes = [Node(endpoint, locations[endpoint.ip]) for endpoint in endpoints if endpoint.ip in locations]
        report["candidates"] = len(endpoints)
        report["calibrated"] = len(nodes)
        plan = cfg["regions"]
        all_regions = "*" in plan
        # IPinfo grouping can be filtered before any probe. Colo grouping MUST
        # use a verified (live or cached) CF-Ray, never IP geodata. Reused
        # successes shortlisted for downloading are checked live again below.
        if cfg["region_basis"] == "ipinfo":
            nodes = [node for node in nodes if all_regions or node.geo.country_code in plan]
        nodes, sample_counts = selection.sample_regions(nodes, cfg["candidates"])
        report["candidate_pool"]["sampled_regions"] = sample_counts
        report["candidate_pool"]["before_zone"] = len(nodes)
        zone_cache = probe.ZoneCache(cfg["zone"], base)
        report["zone_cache"] = zone_cache.stats
        LOG.info("开始 Zone 验证，共 %d 个候选；地区口径=%s", len(nodes), cfg["region_basis"])
        verified, zone_rejected = await probe.verify_all(nodes, cfg["zone"], zone_cache)
        report["rejected"].update(
            {address: {"stage": "zone", "reason": reason} for address, reason in zone_rejected.items()}
        )
        grouped = defaultdict(list)
        for node in verified:
            if cfg["region_basis"] == "colo":
                code = cfg["colo_countries"].get(node.colo, "")
                if not code:
                    report["rejected"][node.endpoint.address] = {
                        "stage": "region",
                        "reason": f"机房 {node.colo or '未知'} 未配置国家映射",
                    }
                    continue
            else:
                code = node.geo.country_code
            if all_regions or code in plan:
                node.region_code = code
                node.region_name = geo.country_name(code)
                grouped[code].append(node)
        codes = sorted(grouped) if all_regions else list(plan)
        raw_counts = Counter(node.geo.country_code for node in nodes)
        selected = []
        for code in codes:
            quota = plan.get(code, plan.get("*", 1))
            regional = sorted(grouped.get(code, []), key=lambda node: (node.latency_ms, node.endpoint))
            status = {
                "requested": quota,
                "zone_passed": len(regional),
                "zone_recheck_failed": 0,
                "speed_tested": 0,
                "speed_passed": 0,
                "selected": 0,
                "shortfall": quota,
            }
            if cfg["region_basis"] == "ipinfo":
                status["calibrated_candidates"] = raw_counts[code]
            report["regions"][code] = status
            maximum = int(cfg["speed"]["candidates_per_region"])
            if maximum:
                regional = regional[: max(quota, maximum)]
            LOG.info(
                "%s：Zone 通过 %d，进入测速 %d，目标 Top %d",
                code,
                status["zone_passed"],
                len(regional),
                quota,
            )
            measured = []
            # Candidate IPs run serially to avoid competing for local bandwidth.
            # Parallelism is INSIDE each IP via speed.streams.
            for node in regional:
                checked, reason = await probe.recheck_cached(node, cfg["zone"], zone_cache)
                if checked is None:
                    status["zone_recheck_failed"] += 1
                    report["rejected"][node.endpoint.address] = {
                        "stage": "zone_recheck",
                        "reason": reason,
                    }
                    LOG.warning("%s %s：缓存通过，但测速前复验失败 (%s)", code, node.endpoint.address, reason)
                    continue
                if cfg["region_basis"] == "colo" and cfg["colo_countries"].get(node.colo) != code:
                    report["rejected"][node.endpoint.address] = {
                        "stage": "region",
                        "reason": f"测速前复验机房变为 {node.colo or '未知'}，不属于当前 {code} 分组",
                    }
                    LOG.warning("%s %s：复验机房变化为 %s，本轮跳过", code, node.endpoint.address, node.colo)
                    continue
                maximum_latency = float(cfg["speed"]["max_latency_ms"])
                if maximum_latency and node.latency_ms > maximum_latency:
                    report["rejected"][node.endpoint.address] = {
                        "stage": "latency",
                        "reason": "响应延迟超过 max_latency_ms",
                    }
                    continue
                status["speed_tested"] += 1
                result, reason = await speed.measure(node, cfg["speed"], cfg["zone"]["verify_tls"], base)
                if result:
                    measured.append(result)
                else:
                    report["rejected"][node.endpoint.address] = {"stage": "speed", "reason": reason}
                    LOG.warning("%s %s：%s", code, node.endpoint.address, reason)
            if cfg["speed"]["sort_by"] == "latency":
                measured.sort(key=lambda node: (node.latency_ms, -node.speed_mbps, node.endpoint))
            else:
                measured.sort(key=lambda node: (-node.speed_mbps, node.latency_ms, node.endpoint))
            winners = measured[:quota]
            status.update(
                speed_passed=len(measured), selected=len(winners), shortfall=max(0, quota - len(winners))
            )
            selected.extend(winners)
            if status["shortfall"]:
                LOG.warning(
                    "%s 配额不足：目标 %d，实际 %d；不会混入其他地区或绕过验证", code, quota, len(winners)
                )
        report["selected"] = [node.to_dict() for node in selected]
        if not selected:
            raise OptimizerError("没有通过验证和测速的节点，保留现有 ip.txt；详情见报告")
        output = resolve_path(base, cfg["output"]["file"])
        atomic_write(
            output, "\n".join(format_node(node, cfg["output"]["include_metrics"]) for node in selected) + "\n"
        )
        report["output_updated"] = True
        LOG.info("已写入 %s，共 %d 个节点", output, len(selected))
        if publish_result and cfg["github"]["enabled"]:
            result = await asyncio.to_thread(publish, output, cfg["github"], base)
            report["github"] = asdict(result)
            if not result.success:
                report["error"] = "本地结果已保存，GitHub 未确认同步成功"
                return 2
        else:
            report["github"] = {"state": "disabled", "success": False}
        report["success"] = True
        return 0
    except OptimizerError as exc:
        report["error"] = str(exc)
        raise
    finally:
        if zone_cache is not None:
            zone_cache.save(force=True)
            LOG.info(
                "Zone 本轮：实际验证 %d，缓存通过节点测速前复验 %d（失败 %d）",
                zone_cache.stats["fresh"],
                zone_cache.stats["rechecked"],
                zone_cache.stats["recheck_failed"],
            )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(report_path, report)
