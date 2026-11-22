from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import geo, probe, proxyip, risk, selection, sources, speed, tcp
from .models import Node
from .publish import publish
from .util import OptimizerError, atomic_write, bounded_map, resolve_path, write_json

LOG = logging.getLogger(__name__)


def latency_key(node: Node) -> tuple:
    return node.latency_ms, node.endpoint


def format_node(node: Node, metrics: bool = True, speed_enabled: bool = True) -> str:
    code = node.region_code or node.geo.country_code
    name = node.region_name or node.geo.country_name
    line = f"{node.endpoint.address}#{code} {name}"
    if metrics:
        line += f" {node.latency_ms:.0f}ms"
        if speed_enabled:
            line += f" {node.speed_mbps:.2f}Mbps"
        if node.colo:
            line += f" colo={node.colo}"
    return line


async def run(cfg: dict, base: Path, publish_result: bool = True) -> int:
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "success": False,
        "output_updated": False,
        "region_basis": cfg["region_basis"],
        "regional_mode": cfg["regional_mode"],
        "speed_enabled": cfg["speed"]["enabled"],
        "proxyip": {"enabled": cfg["proxyip"]["enabled"]},
        "ip_risk": {"enabled": cfg["proxyip"]["enabled"] and cfg["proxyip"]["risk"]["enabled"]},
        "regions": {},
        "rejected": {},
        "selected": [],
        "github": {"state": "not_attempted", "success": False},
    }
    report_path = resolve_path(base, cfg["output"]["report_file"])
    zone_cache = None
    proxy_context = None
    risk_filter = None
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
        regional_mode = cfg["regional_mode"]
        speed_enabled = cfg["speed"]["enabled"]
        all_regions = not regional_mode or "*" in plan
        # IPinfo grouping can be filtered before any probe. Colo grouping MUST
        # use a verified (live or cached) CF-Ray, never IP geodata. Reused
        # successes shortlisted for downloading are checked live again below.
        if cfg["region_basis"] == "ipinfo":
            nodes = [node for node in nodes if all_regions or node.geo.country_code in plan]
        elif regional_mode:
            LOG.info("机房地区需要 Zone 响应才能确定，地区筛选将在 Zone 后执行")
        report["candidate_pool"]["after_region_filter"] = len(nodes)
        raw_counts = Counter(node.geo.country_code for node in nodes)
        report["candidate_pool"]["before_tcp"] = len(nodes)
        tcp_passed, tcp_rejected = await tcp.verify_all(nodes, cfg["tcp"])
        report["tcp"] = {"tested": len(nodes), "passed": len(tcp_passed), "rejected": len(tcp_rejected)}
        report["rejected"].update(
            {address: {"stage": "tcp", "reason": reason} for address, reason in tcp_rejected.items()}
        )
        report["candidate_pool"]["after_tcp"] = len(tcp_passed)
        shortlisted, sample_counts = selection.shortlist_tcp(tcp_passed, cfg["candidates"])
        report["candidate_pool"]["sampled_regions"] = sample_counts
        report["candidate_pool"]["after_tcp_selection"] = len(shortlisted)
        report["candidate_pool"]["before_zone"] = len(shortlisted)
        if cfg["proxyip"]["enabled"] and shortlisted:
            proxy_context = await proxyip.prepare(cfg["proxyip"], cfg["zone"])
            report["proxyip"].update(proxy_context.stats)
        zone_cache = probe.ZoneCache(cfg["zone"], base, proxy_context)
        report["zone_cache"] = zone_cache.stats
        LOG.info("开始 Zone 验证，共 %d 个候选；地区口径=%s", len(shortlisted), cfg["region_basis"])
        if proxy_context is not None:
            verified, zone_rejected = await probe.verify_all(
                shortlisted, cfg["zone"], zone_cache, proxy_context
            )
        else:
            verified, zone_rejected = await probe.verify_all(shortlisted, cfg["zone"], zone_cache)
        report["zone_verified"] = len(verified)
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
                grouped[code if regional_mode else "*"].append(node)
        if proxy_context is not None:
            # Colo countries are known only after Zone. Filter countries before
            # spending risk API quota on exits outside the requested regions.
            verified = [node for group in grouped.values() for node in group]
            filtered = []
            for node in verified:
                reason = proxyip.filter_type(node, cfg["proxyip"]["exit_types"])
                if reason == "ok":
                    filtered.append(node)
                else:
                    report["rejected"][node.endpoint.address] = {"stage": "exit_type", "reason": reason}
            LOG.info("反代出口类型过滤：通过 %d/%d", len(filtered), len(verified))
            verified = filtered
            report["proxyip"]["exit_type_passed"] = len(verified)
            if cfg["proxyip"]["risk"]["enabled"] and verified:
                risk_filter = risk.RiskFilter(cfg["proxyip"]["risk"], base)
                report["ip_risk"] = risk_filter.stats
                verified, risk_rejected = await risk_filter.filter_all(verified)
                report["rejected"].update(
                    {
                        address: {"stage": "risk", "reason": reason}
                        for address, reason in risk_rejected.items()
                    }
                )
            report["proxyip"]["after_filters"] = len(verified)
            grouped = defaultdict(list)
            for node in verified:
                grouped[node.region_code if regional_mode else "*"].append(node)
        codes = (sorted(grouped) if all_regions else list(plan)) if regional_mode else ["*"]
        selected = []
        for code in codes:
            quota = plan.get(code, plan.get("*", 1)) if regional_mode else cfg["top_n"]
            regional = sorted(grouped.get(code, []), key=latency_key)
            status = {
                "requested": quota,
                "zone_passed": len(regional),
                "zone_recheck_failed": 0,
                "proxy_recheck_failed": 0,
                "speed_tested": 0,
                "speed_passed": 0,
                "selected": 0,
                "shortfall": quota,
            }
            if cfg["region_basis"] == "ipinfo":
                status["calibrated_candidates"] = (
                    raw_counts[code] if regional_mode else sum(raw_counts.values())
                )
            report["regions"][code] = status
            maximum = int(cfg["speed"]["candidates_per_region"])
            if speed_enabled and maximum:
                regional = regional[: max(quota, maximum)]
            LOG.info(
                "%s：Zone 通过 %d，待%s %d，目标 Top %d",
                code,
                status["zone_passed"],
                "测速" if speed_enabled else "按延迟选取",
                len(regional),
                quota,
            )

            async def refresh(node):
                if proxy_context is not None:
                    checked, reason = await probe.recheck_cached(node, cfg["zone"], zone_cache, proxy_context)
                else:
                    checked, reason = await probe.recheck_cached(node, cfg["zone"], zone_cache)
                if checked is None:
                    status["zone_recheck_failed"] += 1
                    report["rejected"][node.endpoint.address] = {
                        "stage": "zone_recheck",
                        "reason": reason,
                    }
                    LOG.warning(
                        "%s %s：缓存通过，但下载/输出前复验失败 (%s)", code, node.endpoint.address, reason
                    )
                    return None
                if proxy_context is not None:
                    reason = proxyip.filter_type(node, cfg["proxyip"]["exit_types"])
                    stage = "exit_type_recheck"
                    if reason == "ok" and risk_filter is not None:
                        reason = await risk_filter.filter(node)
                        stage = "risk_recheck"
                    if reason != "ok":
                        status["proxy_recheck_failed"] += 1
                        report["rejected"][node.endpoint.address] = {"stage": stage, "reason": reason}
                        LOG.warning(
                            "%s %s：下载/输出前出口过滤失败 (%s)", code, node.endpoint.address, reason
                        )
                        return None
                if cfg["region_basis"] == "colo":
                    current_code = cfg["colo_countries"].get(node.colo, "")
                    if not current_code or (regional_mode and current_code != code):
                        report["rejected"][node.endpoint.address] = {
                            "stage": "region",
                            "reason": f"复验机房变为 {node.colo or '未知'}，与当前地区分组不匹配",
                        }
                        return None
                    node.region_code = current_code
                    node.region_name = geo.country_name(current_code)
                maximum_latency = float(cfg["speed"]["max_latency_ms"])
                if maximum_latency and node.latency_ms > maximum_latency:
                    report["rejected"][node.endpoint.address] = {
                        "stage": "latency",
                        "reason": "响应延迟超过 max_latency_ms",
                    }
                    return None
                return node

            async def measure(node):
                node = await refresh(node)
                if node is None:
                    return None
                status["speed_tested"] += 1
                result, reason = await speed.measure(node, cfg["speed"], cfg["zone"]["verify_tls"], base)
                if result:
                    return result
                else:
                    report["rejected"][node.endpoint.address] = {"stage": "speed", "reason": reason}
                    LOG.warning("%s %s：%s", code, node.endpoint.address, reason)
                return None

            measured = []
            if speed_enabled:
                results = await bounded_map(measure, regional, cfg["speed"]["concurrency"])
                measured = [node for node in results if node is not None]
                status["speed_passed"] = len(measured)
            else:
                # Cached timings shortlist candidates; only output candidates
                # are rechecked live. Continue past failures to fill the quota.
                for node in regional:
                    result = await refresh(node)
                    if result is not None:
                        measured.append(result)
                    if len(measured) >= quota:
                        break
            if not speed_enabled or cfg["speed"]["sort_by"] == "latency":
                measured.sort(key=lambda node: (node.latency_ms, -node.speed_mbps, node.endpoint))
            else:
                measured.sort(key=lambda node: (-node.speed_mbps, node.latency_ms, node.endpoint))
            winners = measured[:quota]
            status.update(selected=len(winners), shortfall=max(0, quota - len(winners)))
            selected.extend(winners)
            if status["shortfall"]:
                LOG.warning(
                    "%s 配额不足：目标 %d，实际 %d；不会混入其他地区或绕过验证", code, quota, len(winners)
                )
        report["selected"] = [node.to_dict() for node in selected]
        if not selected:
            raise OptimizerError("没有通过本轮筛选的节点，保留现有 ip.txt；详情见报告")
        output = resolve_path(base, cfg["output"]["file"])
        atomic_write(
            output,
            "\n".join(format_node(node, cfg["output"]["include_metrics"], speed_enabled) for node in selected)
            + "\n",
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
        if risk_filter is not None:
            await risk_filter.close()
        if zone_cache is not None:
            zone_cache.save(force=True)
            LOG.info(
                "Zone 本轮：实际验证 %d，缓存通过节点下载/输出前复验 %d（失败 %d）",
                zone_cache.stats["fresh"],
                zone_cache.stats["rechecked"],
                zone_cache.stats["recheck_failed"],
            )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(report_path, report)
