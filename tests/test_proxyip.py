import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from cfoptimizer.config import DEFAULTS, load_config, validate
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.pipeline import run
from cfoptimizer.probe import recheck_cached, verify, verify_all
from cfoptimizer.proxyip import ProxyContext, exit_type, filter_type, normalize_ip, prepare
from cfoptimizer.util import OptimizerError
from cfoptimizer.zone_cache import ZoneCache


def node(ip="192.0.2.1"):
    return Node(Endpoint(ip), Geo("JP", "日本", 0), colo="NRT", status=200, latency_ms=5)


class ProxyConfigTests(unittest.TestCase):
    def config(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"]["domain"] = "zone.invalid"
        cfg["sources"] = ["candidates.txt"]
        return cfg

    def test_parent_disabled_ignores_child_options_and_custom_zone_path(self):
        cfg = self.config()
        cfg["zone"]["path"] = "/"
        cfg["proxyip"].update(baseline_url=None, exit_types="bad", risk=None)
        self.assertFalse(validate(cfg)["proxyip"]["enabled"])

    def test_risk_disabled_does_not_require_its_child_fields(self):
        cfg = self.config()
        cfg["proxyip"]["enabled"] = True
        cfg["proxyip"]["risk"].update(key_file=None, max_level="bad", cache_file=None)
        validate(cfg)

    def test_enabled_proxy_and_risk_reject_invalid_active_settings(self):
        cases = [
            ("proxyip", "enabled", "true"),
            ("zone", "path", "/"),
            ("proxyip", "exit_types", ["ipv4"]),
            ("proxyip", "exit_types", [["ipv4_only"]]),
            ("proxyip", "baseline_url", "http://www.cloudflare.com/cdn-cgi/trace"),
            ("proxyip", "baseline_url", "https://127.0.0.1/cdn-cgi/trace"),
            ("proxyip", "baseline_rounds", True),
            ("risk", "enabled", "true"),
            ("risk", "provider", "ipinfo"),
            ("risk", "key_file", ""),
            ("risk", "max_level", "低风险"),
            ("risk", "unknown_policy", "allow"),
            ("risk", "concurrency", 1.5),
            ("risk", "timeout_seconds", float("nan")),
            ("risk", "retries", -1),
            ("risk", "cache_enabled", 1),
            ("risk", "cache_ttl_hours", float("inf")),
        ]
        for section, key, value in cases:
            cfg = self.config()
            cfg["proxyip"]["enabled"] = True
            cfg["proxyip"]["risk"]["enabled"] = True
            target = cfg["proxyip"]["risk"] if section == "risk" else cfg[section]
            target[key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(OptimizerError):
                validate(cfg)

    def test_active_risk_paths_must_not_overwrite_config_or_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            cfg = self.config()
            cfg["proxyip"]["enabled"] = True
            cfg["proxyip"]["risk"].update(enabled=True, cache_file="ip.txt")
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with self.assertRaises(OptimizerError):
                load_config(path)
            cfg["proxyip"]["enabled"] = False
            path.write_text(json.dumps(cfg), encoding="utf-8")
            load_config(path)

    def test_same_family_baseline_and_mapped_ipv6_are_compared_canonically(self):
        context = ProxyContext({4: {"198.51.100.1", "198.51.100.2"}})
        for ip in ("198.51.100.1", "::ffff:198.51.100.2"):
            self.assertIn("出口未变化", context.check_exits([ip]))
        self.assertEqual(context.check_exits(["198.51.100.3"]), "ok")
        self.assertIn("未取得 IPv6", context.check_exits(["2001:db8::1"]))
        self.assertEqual(normalize_ip("2001:0db8::1"), "2001:db8::1")

    def test_exit_filter_uses_observed_source_family_not_candidate_family(self):
        item = node("2001:db8::1")
        for exits, expected in [
            (["198.51.100.7"], "ipv4_only"),
            (["2001:db8::7"], "ipv6_only"),
            (["198.51.100.7", "2001:db8::7"], "dual_stack"),
        ]:
            item.proxy_exit_ips = exits
            self.assertEqual(exit_type(exits), expected)
            self.assertEqual(filter_type(item, [expected]), "ok")
            self.assertEqual(filter_type(item, []), "ok")
            if expected != "ipv4_only":
                self.assertNotEqual(filter_type(item, ["ipv4_only"]), "ok")


class ProxyNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.exits = ["198.51.100.7", "2001:db8::7"]
        self.calls = 0
        self.risk_calls = 0
        self.download_calls = 0
        self.body_override = None
        self.context = ProxyContext({4: {"198.51.100.1"}, 6: {"2001:db8::1"}})
        app = web.Application()
        app.router.add_get("/cdn-cgi/trace", self.trace)
        app.router.add_post("/risk", self.risk_api)
        app.router.add_get("/download", self.download)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.zone = copy.deepcopy(DEFAULTS["zone"])
        self.zone.update(domain="zone.invalid", scheme="http", rounds=2)

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.tmp.cleanup()

    def candidate(self):
        return Node(Endpoint("127.0.0.1", self.port), Geo("JP", "日本", 0))

    async def trace(self, request):
        ip = self.exits[self.calls % len(self.exits)]
        self.calls += 1
        text = self.body_override or (
            f"h={request.host}\nip={ip}\nuag={request.headers['User-Agent']}\n"
            "visit_scheme=http\ntls=off\ncolo=NRT\n"
        )
        return web.Response(text=text, headers={"Server": "cloudflare", "CF-Ray": "test-NRT"})

    async def risk_api(self, request):
        self.risk_calls += 1
        data = await request.json()
        return web.json_response(
            {
                "ip": data["q"],
                "company": {"abuser_score": "0 (Low)"},
                "asn": {"abuser_score": "0 (Low)"},
                **{
                    key: False
                    for key in ("is_bogon", "is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser")
                },
            }
        )

    async def download(self, request):
        self.download_calls += 1
        return web.Response(body=b"x" * 65536, content_type="application/octet-stream")

    async def test_proxy_check_reuses_zone_rounds_and_collects_all_exits(self):
        result, reason = await verify(self.candidate(), self.zone, self.context)
        self.assertIsNotNone(result, reason)
        self.assertEqual(result.proxy_exit_ips, sorted(self.exits))
        self.assertEqual(result.proxy_exit_type, "dual_stack")
        self.assertEqual(self.calls, 2)

    async def test_any_round_with_unchanged_exit_rejects_candidate(self):
        self.exits[1] = "2001:db8::1"
        result, reason = await verify(self.candidate(), self.zone, self.context)
        self.assertIsNone(result)
        self.assertIn("出口未变化", reason)
        self.assertEqual(self.calls, 2)

    async def test_missing_family_baseline_does_not_pass_by_cross_family_inequality(self):
        result, reason = await verify(self.candidate(), self.zone, ProxyContext({4: {"198.51.100.1"}}))
        self.assertIsNone(result)
        self.assertIn("未取得 IPv6", reason)

    async def test_one_proxy_exit_can_repeat_across_every_round(self):
        self.exits = ["198.51.100.7"]
        result, reason = await verify(self.candidate(), self.zone, self.context)
        self.assertIsNotNone(result, reason)
        self.assertEqual(result.proxy_exit_type, "ipv4_only")
        self.assertEqual(self.calls, 2)

    async def test_normal_mode_accepts_unchanged_exit_without_storing_source_ip(self):
        self.exits = ["198.51.100.1"]
        result, reason = await verify(self.candidate(), self.zone)
        self.assertIsNotNone(result, reason)
        self.assertEqual(result.proxy_exit_ips, [])

    async def test_cache_is_bound_to_mode_and_baseline_but_not_filter_threshold(self):
        cache = ZoneCache(self.zone, self.base, self.context)
        passed, _ = await verify_all([self.candidate()], self.zone, cache, self.context)
        self.assertEqual(len(passed), 1)
        raw = json.loads(cache.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["entries"][passed[0].endpoint.address]["exit_ips"], sorted(self.exits))
        self.assertNotIn("198.51.100.1", cache.path.read_text(encoding="utf-8"))
        restored = ZoneCache(self.zone, self.base, self.context)
        result, _ = restored.lookup(self.candidate())
        self.assertEqual(result.proxy_exit_type, "dual_stack")
        self.assertIsNone(ZoneCache(self.zone, self.base).lookup(self.candidate()))
        changed = ProxyContext({4: {"198.51.100.2"}, 6: {"2001:db8::1"}})
        self.assertIsNone(ZoneCache(self.zone, self.base, changed).lookup(self.candidate()))
        self.assertNotEqual(filter_type(result, ["ipv4_only"]), "ok")
        self.assertEqual(filter_type(result, ["dual_stack"]), "ok")
        self.assertEqual(self.calls, 2)

    async def test_cached_winner_live_recheck_replaces_observed_exit_type(self):
        cache = ZoneCache(self.zone, self.base, self.context)
        await verify_all([self.candidate()], self.zone, cache, self.context)
        cache = ZoneCache(self.zone, self.base, self.context)
        cached, _ = cache.lookup(self.candidate())
        self.exits = ["198.51.100.8"]
        checked, reason = await recheck_cached(cached, self.zone, cache, self.context)
        self.assertIsNotNone(checked, reason)
        self.assertEqual(checked.proxy_exit_ips, ["198.51.100.8"])
        self.assertEqual(checked.proxy_exit_type, "ipv4_only")
        self.assertEqual(self.calls, 4)

    async def test_baseline_uses_direct_unpinned_dns_and_no_proxy_environment(self):
        self.exits = ["198.51.100.1"]
        cfg = copy.deepcopy(DEFAULTS["proxyip"])
        cfg["baseline_url"] = f"http://localhost:{self.port}/cdn-cgi/trace"
        # Replacing the mapping preserves empty native environment values on Windows.
        with patch.object(os, "environ", dict(os.environ, HTTP_PROXY="http://127.0.0.1:1")):
            context = await prepare(cfg, self.zone)
        self.assertEqual(context.baselines, {4: {"198.51.100.1"}})
        self.assertEqual(context.stats["baseline_requests"], 4)
        self.assertEqual(self.calls, 2)

    async def test_invalid_baseline_trace_does_not_produce_a_context(self):
        cfg = copy.deepcopy(DEFAULTS["proxyip"])
        cfg["baseline_url"] = f"http://localhost:{self.port}/cdn-cgi/trace"
        self.body_override = "<html>invalid</html>"
        with self.assertRaises(OptimizerError):
            await prepare(cfg, self.zone)

    async def test_real_pipeline_with_proxy_filters_download_and_cache_recheck(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"].update(self.zone)
        cfg["sources"] = ["candidates.txt"]
        cfg["regions"] = {"JP": 1}
        cfg["proxyip"].update(enabled=True, exit_types=["dual_stack"])
        cfg["proxyip"]["risk"]["enabled"] = True
        cfg["speed"].update(url="http://zone.invalid/download", max_bytes=65536, streams=1, repeats=1)
        (self.base / "candidates.txt").write_text(f"127.0.0.1:{self.port}\n", encoding="utf-8")
        (self.base / "ipapi_key.txt").write_text("TEST_KEY", encoding="utf-8")
        state = self.base / ".state"
        state.mkdir()
        import time

        (state / "ipinfo_lite.json").write_text(
            json.dumps({"127.0.0.1": {"country_code": "JP", "updated_at": time.time()}})
        )
        with (
            patch("cfoptimizer.pipeline.proxyip.prepare", AsyncMock(return_value=self.context)),
            patch("cfoptimizer.risk.API_URL", f"http://127.0.0.1:{self.port}/risk"),
        ):
            self.assertEqual(await run(cfg, self.base), 0)
            self.assertEqual((self.calls, self.risk_calls, self.download_calls), (2, 2, 1))
            self.assertEqual(await run(cfg, self.base), 0)
            self.assertEqual((self.calls, self.risk_calls, self.download_calls), (4, 2, 2))
            before = (self.base / "ip.txt").read_bytes()
            self.exits = ["198.51.100.8"]
            with self.assertRaises(OptimizerError):
                await run(cfg, self.base)
        self.assertEqual((self.calls, self.risk_calls, self.download_calls), (6, 2, 2))
        self.assertEqual((self.base / "ip.txt").read_bytes(), before)
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rejected"][f"127.0.0.1:{self.port}"]["stage"], "exit_type_recheck")


class ProxyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["zone"]["domain"] = "zone.invalid"
        self.cfg["regions"] = {"JP": 1}
        self.cfg["speed"]["enabled"] = False
        self.item = node()
        self.context = ProxyContext({4: {"198.51.100.1"}, 6: {"2001:db8::1"}})
        self.patches = [
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=[self.item.endpoint])),
            patch(
                "cfoptimizer.pipeline.geo.calibrate",
                AsyncMock(return_value={self.item.endpoint.ip: self.item.geo}),
            ),
            patch(
                "cfoptimizer.pipeline.tcp.verify_all", AsyncMock(side_effect=lambda nodes, cfg: (nodes, {}))
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_disabled_parent_skips_all_proxy_children_even_if_risk_is_enabled(self):
        self.cfg["proxyip"].update(exit_types=["dual_stack"], baseline_url=None)
        self.cfg["proxyip"]["risk"].update(enabled=True, key_file=None)
        with (
            patch(
                "cfoptimizer.pipeline.probe.verify_all",
                AsyncMock(side_effect=lambda nodes, cfg, cache: (nodes, {})),
            ),
            patch(
                "cfoptimizer.pipeline.proxyip.prepare", AsyncMock(side_effect=AssertionError("no baseline"))
            ),
            patch("cfoptimizer.pipeline.proxyip.filter_type", side_effect=AssertionError("no exit filter")),
            patch("cfoptimizer.pipeline.risk.RiskFilter", side_effect=AssertionError("no risk")),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["ip_risk"]["enabled"])

    async def test_type_rejection_happens_before_risk_and_download_or_push(self):
        self.cfg["proxyip"].update(enabled=True, exit_types=["dual_stack"])
        self.cfg["proxyip"]["risk"]["enabled"] = True
        self.cfg["speed"]["enabled"] = True
        (self.base / "ip.txt").write_text("old", encoding="utf-8")

        async def zone(nodes, cfg, cache, proxy):
            nodes[0].proxy_exit_ips = ["198.51.100.7"]
            return nodes, {}

        with (
            patch("cfoptimizer.pipeline.proxyip.prepare", AsyncMock(return_value=self.context)),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch("cfoptimizer.pipeline.risk.RiskFilter", side_effect=AssertionError("no risk query")),
            patch("cfoptimizer.pipeline.speed.measure", AsyncMock(side_effect=AssertionError("no download"))),
            patch("cfoptimizer.pipeline.publish", side_effect=AssertionError("no push")),
        ):
            with self.assertRaises(OptimizerError):
                await run(self.cfg, self.base)
        self.assertEqual((self.base / "ip.txt").read_text(), "old")
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rejected"][self.item.endpoint.address]["stage"], "exit_type")

    async def test_new_exit_after_live_recheck_is_risk_filtered_before_output(self):
        self.cfg["proxyip"]["enabled"] = True
        self.cfg["proxyip"]["risk"]["enabled"] = True
        self.item.proxy_exit_ips = ["198.51.100.7"]
        cache = ZoneCache(self.cfg["zone"], self.base, self.context)
        cache.record(self.item, self.item, "ok")
        cache.save(force=True)
        (self.base / "ip.txt").write_text("old", encoding="utf-8")
        queried = []

        async def fetch(ip):
            queried.append(ip)
            score = 30 if ip.endswith(".8") else 0
            return {"level": "高风险" if score else "极度纯净", "score": score, "reason": "ok"}

        async def live(item, cfg, proxy):
            item.proxy_exit_ips = ["198.51.100.8"]
            return item, "ok"

        with (
            patch("cfoptimizer.pipeline.proxyip.prepare", AsyncMock(return_value=self.context)),
            patch("cfoptimizer.probe.verify", live),
            patch("cfoptimizer.risk.RiskFilter._fetch", AsyncMock(side_effect=fetch)),
        ):
            with self.assertRaises(OptimizerError):
                await run(self.cfg, self.base)
        self.assertEqual(queried, ["198.51.100.7", "198.51.100.8"])
        self.assertEqual((self.base / "ip.txt").read_text(), "old")
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["rejected"][self.item.endpoint.address]["stage"], "risk_recheck")

    async def test_risk_disabled_skips_keys_but_still_applies_type_filter(self):
        self.cfg["proxyip"].update(enabled=True, exit_types=["ipv4_only"])

        async def zone(nodes, cfg, cache, proxy):
            nodes[0].proxy_exit_ips = ["198.51.100.7"]
            return nodes, {}

        with (
            patch("cfoptimizer.pipeline.proxyip.prepare", AsyncMock(return_value=self.context)),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch("cfoptimizer.pipeline.risk.RiskFilter", side_effect=AssertionError("no risk")),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)

    async def test_colo_country_filter_precedes_risk_queries(self):
        self.cfg.update(region_basis="colo", regions={"SG": 1})
        self.cfg["proxyip"]["enabled"] = True
        self.cfg["proxyip"]["risk"]["enabled"] = True

        async def zone(nodes, cfg, cache, proxy):
            nodes[0].colo = "NRT"
            nodes[0].proxy_exit_ips = ["198.51.100.7"]
            return nodes, {}

        with (
            patch("cfoptimizer.pipeline.proxyip.prepare", AsyncMock(return_value=self.context)),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch(
                "cfoptimizer.pipeline.risk.RiskFilter",
                side_effect=AssertionError("not a requested colo country"),
            ),
        ):
            with self.assertRaises(OptimizerError):
                await run(self.cfg, self.base)


class WorkerPoolCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_cancels_other_inflight_network_work(self):
        from cfoptimizer.util import bounded_map
        import asyncio

        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def worker(value):
            if value == 0:
                await started.wait()
                raise OptimizerError("missing key")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with self.assertRaises(OptimizerError):
            await bounded_map(worker, [0, 1], 2)
        self.assertTrue(cleaned.is_set())
