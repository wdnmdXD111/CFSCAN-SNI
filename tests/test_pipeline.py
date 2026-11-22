import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from cfoptimizer.config import DEFAULTS
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.pipeline import run
from cfoptimizer.publish import SyncResult
from cfoptimizer.zone_cache import ZoneCache
from cfoptimizer.util import OptimizerError


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["zone"].update(domain="test.invalid", path="/", scheme="http", rounds=1)
        self.cfg["sources"] = ["candidates.txt"]
        self.cfg["regions"] = {"JP": 1, "SG": 1}
        self.endpoints = [Endpoint("192.0.2.1"), Endpoint("192.0.2.2"), Endpoint("192.0.2.3")]
        self.geos = {
            "192.0.2.1": Geo("JP", "日本", time.time()),
            "192.0.2.2": Geo("JP", "日本", time.time()),
            "192.0.2.3": Geo("SG", "新加坡", time.time()),
        }
        self.tcp_connect = patch("cfoptimizer.tcp.connect", AsyncMock(return_value=10.0))
        self.tcp_connect.start()
        self.addCleanup(self.tcp_connect.stop)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_region_quotas_no_failed_probe_is_speed_tested(self):
        async def fake_probe(nodes, cfg, cache):
            return nodes[1:], {nodes[0].endpoint.address: "CF 1034"}

        measured = []

        async def fake_speed(node, cfg, verify_tls, base):
            self.assertEqual(base, self.base)
            measured.append(node.endpoint)
            node.speed_mbps = 10
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", fake_probe),
            patch("cfoptimizer.pipeline.speed.measure", fake_speed),
        ):
            code = await run(self.cfg, self.base)
        self.assertEqual(code, 0)
        self.assertNotIn(self.endpoints[0], measured)
        output = (self.base / "ip.txt").read_text(encoding="utf-8")
        self.assertIn("#JP 日本", output)
        self.assertIn("#SG 新加坡", output)
        self.assertEqual(len(output.splitlines()), 2)

    async def test_no_results_preserves_previous_output_and_reports_shortfall(self):
        (self.base / "ip.txt").write_text("previous results", encoding="utf-8")
        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", AsyncMock(return_value=([], {}))),
        ):
            with self.assertRaises(OptimizerError):
                await run(self.cfg, self.base)
        self.assertEqual((self.base / "ip.txt").read_text(), "previous results")
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["success"])
        self.assertEqual(report["regions"]["JP"]["shortfall"], 1)

    async def test_colo_grouping_uses_probe_not_lite_country(self):
        self.cfg.update(region_basis="colo", regions={"SG": 1})

        async def fake_probe(nodes, cfg, cache):
            self.assertEqual(len(nodes), 3)  # Cannot prefilter by Lite country.
            nodes[0].colo = "SIN"
            return nodes[:1], {}

        async def fake_speed(node, cfg, verify_tls, base):
            node.speed_mbps = 10
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", fake_probe),
            patch("cfoptimizer.pipeline.speed.measure", fake_speed),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
        output = (self.base / "ip.txt").read_text(encoding="utf-8")
        self.assertIn("192.0.2.1:443#SG 新加坡", output)
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["selected"][0]["geo"]["country_code"], "JP")

    async def test_failed_publish_returns_two_despite_local_success(self):
        self.cfg["github"]["enabled"] = True
        node = Node(self.endpoints[0], self.geos["192.0.2.1"])
        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", AsyncMock(return_value=([node], {}))),
            patch(
                "cfoptimizer.pipeline.speed.measure",
                AsyncMock(side_effect=lambda node, cfg, verify, base: (node, "ok")),
            ),
            patch("cfoptimizer.pipeline.publish", return_value=SyncResult(False, "rejected")),
        ):
            self.assertEqual(await run(self.cfg, self.base), 2)
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["output_updated"])
        self.assertFalse(report["success"])
        self.assertFalse(report["github"]["success"])

    async def test_exact_stage_order_and_tcp_failure_never_reaches_zone(self):
        self.cfg["regions"] = {"JP": 1}
        self.cfg["candidates"]["ports"] = [443]
        blocked_port = Endpoint("192.0.2.4", 8443)
        calls = []

        async def collect(cfg, base):
            calls.append("sources")
            return [*self.endpoints, blocked_port]

        async def calibrate(endpoints, cfg, base):
            calls.append("lite")
            self.assertEqual(endpoints, self.endpoints)
            return self.geos

        async def tcp(nodes, cfg):
            calls.append("tcp")
            self.assertEqual([node.endpoint for node in nodes], self.endpoints[:2])
            return nodes[1:], {nodes[0].endpoint.address: "TCP 无成功连接"}

        async def zone(nodes, cfg, cache):
            calls.append("zone")
            self.assertEqual([node.endpoint for node in nodes], self.endpoints[1:2])
            return nodes, {}

        async def speed(node, cfg, verify_tls, base):
            calls.append("speed")
            return node, "ok"

        def publish(output, cfg, base):
            calls.append("publish")
            self.assertIn("192.0.2.2:443", output.read_text(encoding="utf-8"))
            return SyncResult(True, "confirmed")

        self.cfg["github"]["enabled"] = True
        with (
            patch("cfoptimizer.pipeline.sources.collect", collect),
            patch("cfoptimizer.pipeline.geo.calibrate", calibrate),
            patch("cfoptimizer.pipeline.tcp.verify_all", tcp),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch("cfoptimizer.pipeline.speed.measure", speed),
            patch("cfoptimizer.pipeline.publish", publish),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
        self.assertEqual(calls, ["sources", "lite", "tcp", "zone", "speed", "publish"])
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["tcp"], {"tested": 2, "passed": 1, "rejected": 1})
        self.assertEqual(report["rejected"][self.endpoints[0].address]["stage"], "tcp")

    async def test_global_mode_ignores_region_plan_and_uses_one_top_n_quota(self):
        self.cfg.update(regional_mode=False, regions={"US": 1}, top_n=2)
        tested = []

        async def zone(nodes, cfg, cache):
            for node, latency in zip(nodes, [30, 20, 10]):
                node.latency_ms = latency
            return nodes, {}

        async def speed(node, cfg, verify_tls, base):
            tested.append(node.endpoint)
            node.speed_mbps = {"192.0.2.1": 30, "192.0.2.2": 20, "192.0.2.3": 40}[node.endpoint.ip]
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch("cfoptimizer.pipeline.speed.measure", speed),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
        self.assertEqual(set(tested), set(self.endpoints))
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(list(report["regions"]), ["*"])
        self.assertEqual([node["endpoint"]["ip"] for node in report["selected"]], ["192.0.2.3", "192.0.2.1"])
        self.assertEqual([node["region_code"] for node in report["selected"]], ["SG", "JP"])

    async def test_disabled_speed_skips_download_token_and_speed_threshold(self):
        self.cfg.update(regional_mode=False, top_n=2)
        self.cfg["speed"].update(enabled=False, min_mbps=999999, candidates_per_region=1)
        (self.base / "speed_token.txt").write_text("invalid\nmultiple\npasswords", encoding="utf-8")

        async def zone(nodes, cfg, cache):
            for node, latency in zip(nodes, [30, 20, 10]):
                node.latency_ms = latency
            return nodes, {}

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.pipeline.probe.verify_all", zone),
            patch("cfoptimizer.pipeline.speed.measure", AsyncMock()) as speed,
            patch("cfoptimizer.pipeline.publish") as publish,
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
            speed.assert_not_called()
            publish.assert_not_called()
        output = (self.base / "ip.txt").read_text(encoding="utf-8")
        self.assertNotIn("Mbps", output)
        self.assertIn("192.0.2.3:443#SG", output.splitlines()[0])
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["speed_enabled"])
        self.assertEqual(report["regions"]["*"]["speed_tested"], 0)

    async def test_disabled_speed_rechecks_cached_output_and_replaces_a_failed_shortlist(self):
        self.cfg["regions"] = {"JP": 1}
        self.cfg["speed"]["enabled"] = False
        cache = ZoneCache(self.cfg["zone"], self.base)
        for index, endpoint in enumerate(self.endpoints[:2]):
            node = Node(endpoint, self.geos[endpoint.ip], colo="NRT", status=200, latency_ms=index + 1)
            cache.record(node, node, "ok")
        cache.save(force=True)

        async def live(node, cfg):
            if node.endpoint == self.endpoints[0]:
                return None, "CF 1034"
            node.latency_ms = 10
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints[:2])),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch("cfoptimizer.probe.verify", live),
            patch("cfoptimizer.pipeline.speed.measure", AsyncMock()) as speed,
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
            speed.assert_not_called()
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["zone_cache"]["rechecked"], 2)
        self.assertEqual(report["zone_cache"]["recheck_failed"], 1)
        self.assertEqual(report["selected"][0]["endpoint"]["ip"], "192.0.2.2")

    async def test_speed_candidate_concurrency_is_configurable_and_bounded(self):
        self.cfg.update(regional_mode=False, top_n=1)
        self.cfg["speed"].update(concurrency=2, candidates_per_region=0)
        active = peak = 0

        async def speed(node, cfg, verify_tls, base):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            node.speed_mbps = 10
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value=self.geos)),
            patch(
                "cfoptimizer.pipeline.probe.verify_all",
                AsyncMock(side_effect=lambda nodes, cfg, cache: (nodes, {})),
            ),
            patch("cfoptimizer.pipeline.speed.measure", speed),
        ):
            self.assertEqual(await run(self.cfg, self.base), 0)
        self.assertEqual(peak, 2)

    async def test_real_local_end_to_end_with_lite_cache(self):
        self.tcp_connect.stop()
        app = web.Application()

        async def zone(request):
            return web.Response(text="ready", headers={"Server": "cloudflare", "CF-Ray": "example-NRT"})

        async def download(request):
            return web.Response(body=b"x" * 262144, content_type="application/octet-stream")

        app.router.add_get("/", zone)
        app.router.add_get("/download", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.cfg["regions"] = {"JP": 1}
        self.cfg["speed"].update(
            url="http://test.invalid/download", duration_seconds=0.1, max_bytes=524288, streams=2, repeats=1
        )
        (self.base / "candidates.txt").write_text(f"127.0.0.1:{port}\n")
        cache = self.base / ".state" / "ipinfo_lite.json"
        cache.parent.mkdir()
        cache.write_text(json.dumps({"127.0.0.1": {"country_code": "JP", "updated_at": time.time()}}))
        try:
            self.assertEqual(await run(self.cfg, self.base), 0)
        finally:
            await runner.cleanup()
        self.assertIn(f"127.0.0.1:{port}#JP 日本", (self.base / "ip.txt").read_text(encoding="utf-8"))

    async def test_real_two_runs_without_zone_cache_or_downloads(self):
        self.tcp_connect.stop()
        requests = {"zone": 0, "download": 0}
        app = web.Application()

        async def zone(request):
            requests["zone"] += 1
            return web.Response(text="ready", headers={"Server": "cloudflare", "CF-Ray": "example-NRT"})

        async def download(request):
            requests["download"] += 1
            return web.Response(body=b"x" * 65536)

        app.router.add_get("/", zone)
        app.router.add_get("/download", download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.cfg["regions"] = {"JP": 1}
        self.cfg["zone"].update(rounds=2, cache_enabled=False)
        self.cfg["speed"].update(enabled=False, url="http://test.invalid/download")
        (self.base / "candidates.txt").write_text(f"127.0.0.1:{port}\n", encoding="utf-8")
        try:
            with patch(
                "cfoptimizer.pipeline.geo.calibrate",
                AsyncMock(return_value={"127.0.0.1": Geo("JP", "日本", 0)}),
            ):
                for _ in range(2):
                    self.assertEqual(await run(self.cfg, self.base), 0)
        finally:
            await runner.cleanup()
        self.assertEqual(requests, {"zone": 4, "download": 0})
        self.assertFalse((self.base / self.cfg["zone"]["cache_file"]).exists())
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["zone_cache"]["enabled"])
        self.assertEqual(report["zone_cache"]["fresh"], 1)
        self.assertEqual(report["tcp"]["passed"], 1)
        self.assertGreater(report["selected"][0]["tcp_latency_ms"], 0)
        self.assertEqual(report["selected"][0]["speed_samples"], [])
