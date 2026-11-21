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
from cfoptimizer.util import OptimizerError


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["zone"].update(domain="test.invalid", scheme="http", rounds=1)
        self.cfg["sources"] = ["candidates.txt"]
        self.cfg["regions"] = {"JP": 1, "SG": 1}
        self.endpoints = [Endpoint("192.0.2.1"), Endpoint("192.0.2.2"), Endpoint("192.0.2.3")]
        self.geos = {
            "192.0.2.1": Geo("JP", "日本", time.time()),
            "192.0.2.2": Geo("JP", "日本", time.time()),
            "192.0.2.3": Geo("SG", "新加坡", time.time()),
        }

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

    async def test_real_local_end_to_end_with_lite_cache(self):
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
