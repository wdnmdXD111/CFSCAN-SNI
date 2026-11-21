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
from cfoptimizer.probe import recheck_cached, verify_all
from cfoptimizer.util import OptimizerError
from cfoptimizer.zone_cache import ZoneCache


def make_node(ip="192.0.2.1", port=443, colo="NRT"):
    return Node(Endpoint(ip, port), Geo("JP", "日本", 0), colo=colo, status=200, latency_ms=10, jitter_ms=1)


class ZoneCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS["zone"])
        self.cfg["domain"] = "zone.invalid"

    def tearDown(self):
        self.tmp.cleanup()

    def saved_cache(self):
        cache = ZoneCache(self.cfg, self.base)
        passed, failed = make_node(), make_node("2001:db8::1", 8443)
        cache.record(passed, passed, "ok")
        cache.record(failed, None, "CF 1034")
        cache.save(force=True)
        return cache

    def test_persists_success_and_failure_scoped_to_ip_and_port(self):
        old = self.saved_cache()
        cache = ZoneCache(self.cfg, self.base)
        node = make_node()
        node.colo, node.status, node.latency_ms = "", 0, 0
        passed, reason = cache.lookup(node)
        self.assertIs(passed, node)
        self.assertEqual((reason, node.colo, node.status, node.latency_ms), ("ok", "NRT", 200, 10))
        self.assertEqual(cache.lookup(make_node("2001:db8::1", 8443)), (None, "CF 1034"))
        self.assertIsNone(cache.lookup(make_node(port=8443)))
        raw = json.loads(old.path.read_text(encoding="utf-8"))
        self.assertIn("[2001:db8::1]:8443", raw["entries"])
        self.assertNotIn("domain", raw)  # Rule values and page markers need not be stored verbatim.
        self.assertEqual(list(old.path.parent.glob("*.tmp")), [])

    def test_config_changes_invalidate_cache_but_concurrency_and_order_do_not(self):
        self.saved_cache()
        changes = {
            "domain": "another.invalid",
            "path": "/probe",
            "scheme": "http",
            "verify_tls": False,
            "timeout_seconds": 10,
            "connect_timeout_seconds": 4,
            "rounds": 1,
            "accepted_statuses": [200],
            "require_cf_headers": False,
            "expected_body": "new-marker",
            "allowed_colos": ["SIN"],
        }
        for key, value in changes.items():
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = value
            with self.subTest(key=key):
                self.assertIsNone(ZoneCache(cfg, self.base).lookup(make_node()))
        cfg = copy.deepcopy(self.cfg)
        cfg.update(concurrency=1)
        cfg["accepted_statuses"].reverse()
        self.assertIsNotNone(ZoneCache(cfg, self.base).lookup(make_node()))

    def test_success_and_failure_have_independent_expiry_without_sliding_refresh(self):
        old = self.saved_cache()
        raw = json.loads(old.path.read_text(encoding="utf-8"))
        stamp = time.time() - 1800
        for entry in raw["entries"].values():
            entry["updated_at"] = stamp
        old.path.write_text(json.dumps(raw), encoding="utf-8")
        cache = ZoneCache(self.cfg, self.base)
        self.assertIsNotNone(cache.lookup(make_node()))
        self.assertEqual(cache.entries[make_node().endpoint.address]["updated_at"], stamp)
        self.assertIsNone(cache.lookup(make_node("2001:db8::1", 8443)))
        for entry in raw["entries"].values():
            entry["updated_at"] = time.time() - 7 * 3600
        old.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(ZoneCache(self.cfg, self.base).entries, {})

    def test_zero_success_ttl_disables_cache_and_zero_failure_ttl_disables_negative_entries(self):
        old = self.saved_cache()
        before = old.path.read_bytes()
        self.cfg["cache_ttl_hours"] = 0
        cache = ZoneCache(self.cfg, self.base)
        self.assertIsNone(cache.lookup(make_node()))
        cache.record(make_node(), make_node(), "ok")
        cache.save(force=True)
        self.assertEqual(old.path.read_bytes(), before)
        self.cfg.update(cache_ttl_hours=6, cache_failure_ttl_hours=0)
        cache = ZoneCache(self.cfg, self.base)
        self.assertIsNone(cache.lookup(make_node("2001:db8::1", 8443)))
        self.assertIsNotNone(cache.lookup(make_node()))

    def test_corrupt_expired_future_and_nonfinite_cache_entries_are_ignored(self):
        cache = self.saved_cache()
        raw = json.loads(cache.path.read_text(encoding="utf-8"))
        address = make_node().endpoint.address
        for field, value in [
            ("updated_at", time.time() + 1000),
            ("updated_at", True),
            ("updated_at", float("nan")),
            ("latency_ms", float("inf")),
            ("latency_ms", -1),
            ("latency_ms", 10**400),
            ("status", 500),
            ("status", True),
            ("colo", None),
            ("passed", "true"),
            ("reason", ""),
        ]:
            modified = copy.deepcopy(raw)
            modified["entries"][address][field] = value
            cache.path.write_text(json.dumps(modified), encoding="utf-8")
            with self.subTest(field=field, value=str(value)[:60]):
                self.assertIsNone(ZoneCache(self.cfg, self.base).lookup(make_node()))
        for value in ("bad json", "[]", '{"version":true}', json.dumps({"version": 1, "entries": []})):
            cache.path.write_text(value, encoding="utf-8")
            self.assertEqual(ZoneCache(self.cfg, self.base).entries, {})

    def test_write_failure_is_reported_and_does_not_change_verification_result(self):
        cache = ZoneCache(self.cfg, self.base)
        node = make_node()
        cache.record(node, node, "ok")
        with (
            patch("cfoptimizer.zone_cache.write_json", side_effect=OSError("read-only")),
            self.assertLogs("cfoptimizer.zone_cache", "WARNING"),
        ):
            cache.save(force=True)
        self.assertEqual(cache.stats["write_errors"], 1)
        self.assertIs(cache.lookup(make_node())[0].__class__, Node)


class ZoneCacheAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS["zone"])
        self.cfg.update(domain="zone.invalid", scheme="http", rounds=1, concurrency=1)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_cached_failure_is_not_reprobed_and_cached_success_is_rechecked_only_before_speed(self):
        cache = ZoneCache(self.cfg, self.base)
        cache.record(make_node(), make_node(), "ok")
        cache.record(make_node(port=8443), None, "CF 1034")
        cache.save(force=True)
        cache = ZoneCache(self.cfg, self.base)
        with patch(
            "cfoptimizer.probe.verify", AsyncMock(return_value=(None, "ConnectionTimeoutError"))
        ) as verify:
            passed, rejected = await verify_all([make_node(), make_node(port=8443)], self.cfg, cache)
            verify.assert_not_called()
            self.assertEqual(len(passed), 1)
            self.assertEqual(rejected, {make_node(port=8443).endpoint.address: "CF 1034"})
            self.assertEqual(
                await recheck_cached(passed[0], self.cfg, cache), (None, "ConnectionTimeoutError")
            )
            verify.assert_awaited_once()
        cache.save(force=True)
        self.assertEqual(ZoneCache(self.cfg, self.base).lookup(make_node()), (None, "ConnectionTimeoutError"))
        self.assertEqual(cache.stats["rechecked"], 1)
        self.assertEqual(cache.stats["recheck_failed"], 1)

    async def test_cancellation_preserves_only_completed_validation_results(self):
        second_started = asyncio.Event()
        calls = 0

        async def verify(node, cfg):
            nonlocal calls
            calls += 1
            if calls == 1:
                return node, "ok"
            second_started.set()
            await asyncio.Future()

        cache = ZoneCache(self.cfg, self.base)
        with patch("cfoptimizer.probe.verify", verify):
            task = asyncio.create_task(verify_all([make_node(), make_node(port=8443)], self.cfg, cache))
            await asyncio.wait_for(second_started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        restored = ZoneCache(self.cfg, self.base)
        self.assertIsNotNone(restored.lookup(make_node()))
        self.assertIsNone(restored.lookup(make_node(port=8443)))

    async def test_real_network_reuses_both_results_and_rechecks_only_selected_success(self):
        requests = []
        app = web.Application()
        bad_port = None

        async def handler(request):
            port = request.transport.get_extra_info("sockname")[1]
            requests.append(port)
            text = "Error 1034" if port == bad_port else "ready"
            return web.Response(text=text, headers={"Server": "cloudflare", "CF-Ray": "test-NRT"})

        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        sites = [web.TCPSite(runner, "127.0.0.1", 0) for _ in range(2)]
        for site in sites:
            await site.start()
        good_port, bad_port = [site._server.sockets[0].getsockname()[1] for site in sites]

        def nodes():
            return [make_node("127.0.0.1", port) for port in (good_port, bad_port)]

        try:
            passed, rejected = await verify_all(nodes(), self.cfg, ZoneCache(self.cfg, self.base))
            self.assertEqual((len(passed), len(rejected), len(requests)), (1, 1, 2))
            cache = ZoneCache(self.cfg, self.base)
            passed, rejected = await verify_all(nodes(), self.cfg, cache)
            self.assertEqual((len(passed), len(rejected), len(requests)), (1, 1, 2))
            node, reason = await recheck_cached(passed[0], self.cfg, cache)
            self.assertIsNotNone(node, reason)
            self.assertEqual(len(requests), 3)
        finally:
            await runner.cleanup()

    async def test_pipeline_does_not_download_a_cached_success_that_now_fails_zone(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"].update(self.cfg)
        cfg["regions"] = {"JP": 1}
        node = make_node()
        cache = ZoneCache(cfg["zone"], self.base)
        cache.record(node, node, "ok")
        cache.save(force=True)
        (self.base / "ip.txt").write_text("previous", encoding="utf-8")
        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=[node.endpoint])),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value={node.endpoint.ip: node.geo})),
            patch("cfoptimizer.probe.verify", AsyncMock(return_value=(None, "CF 1034"))),
            patch("cfoptimizer.pipeline.speed.measure", AsyncMock()) as measure,
        ):
            with self.assertRaises(OptimizerError):
                await run(cfg, self.base)
            measure.assert_not_called()
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["output_updated"])
        self.assertEqual(report["zone_cache"]["recheck_failed"], 1)
        self.assertEqual((self.base / "ip.txt").read_text(), "previous")

    async def test_colo_change_on_recheck_is_not_published_as_the_cached_country(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"].update(self.cfg)
        cfg.update(region_basis="colo", regions={"JP": 1})
        node = make_node()
        cache = ZoneCache(cfg["zone"], self.base)
        cache.record(node, node, "ok")
        cache.save(force=True)

        async def changed(node, cfg):
            node.colo = "SIN"
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=[node.endpoint])),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value={node.endpoint.ip: node.geo})),
            patch("cfoptimizer.probe.verify", changed),
            patch("cfoptimizer.pipeline.speed.measure", AsyncMock()) as measure,
        ):
            with self.assertRaises(OptimizerError):
                await run(cfg, self.base)
            measure.assert_not_called()
        self.assertEqual(ZoneCache(cfg["zone"], self.base).lookup(make_node())[0].colo, "SIN")

    async def test_two_real_pipeline_runs_reduce_bulk_zone_requests_and_keep_live_downloads(self):
        requests = {"zone": 0, "download": 0}
        app = web.Application()

        async def zone(request):
            requests["zone"] += 1
            return web.Response(text="ready", headers={"Server": "cloudflare", "CF-Ray": "test-NRT"})

        async def download(request):
            requests["download"] += 1
            return web.Response(body=b"x" * 131072, content_type="application/octet-stream")

        app.router.add_get("/", zone)
        app.router.add_get("/download", download)
        runner = web.AppRunner(app)
        await runner.setup()
        sites = [web.TCPSite(runner, "127.0.0.1", 0) for _ in range(3)]
        for site in sites:
            await site.start()
        ports = [site._server.sockets[0].getsockname()[1] for site in sites]
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"].update(self.cfg)
        cfg.update(sources=["candidates.txt"], regions={"JP": 1})
        cfg["candidates"]["ports"] = ports
        cfg["speed"].update(
            url="http://download.invalid/download",
            candidates_per_region=1,
            duration_seconds=0.1,
            max_bytes=65536,
            streams=1,
            repeats=1,
        )
        (self.base / "candidates.txt").write_text(
            "\n".join(f"127.0.0.1:{port}" for port in [*ports, 1]), encoding="utf-8"
        )
        cache_path = self.base / ".state" / "ipinfo_lite.json"
        cache_path.parent.mkdir()
        cache_path.write_text(
            json.dumps({"127.0.0.1": {"country_code": "JP", "updated_at": time.time()}}), encoding="utf-8"
        )
        try:
            self.assertEqual(await run(cfg, self.base), 0)
            self.assertEqual(requests, {"zone": 3, "download": 1})
            self.assertEqual(await run(cfg, self.base), 0)
            self.assertEqual(requests, {"zone": 4, "download": 2})
            report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["candidate_pool"]["source_candidates"], 4)
            self.assertEqual(report["candidate_pool"]["after_port_filter"], 3)
            self.assertEqual(report["zone_cache"]["fresh"], 0)
            self.assertEqual(report["zone_cache"]["hit_passed"], 3)
            self.assertEqual(report["zone_cache"]["rechecked"], 1)
            self.assertEqual(len((self.base / "ip.txt").read_text(encoding="utf-8").splitlines()), 1)
        finally:
            await runner.cleanup()
