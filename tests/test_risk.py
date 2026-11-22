import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from cfoptimizer.config import DEFAULTS
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.risk import LEVELS, RiskFilter, parse_risk, risk_level
from cfoptimizer.util import OptimizerError


def payload(ip="198.51.100.7", score="0 (Low)", **changes):
    result = {
        "ip": ip,
        "company": {"abuser_score": score},
        "asn": {"abuser_score": score},
        **{key: False for key in ("is_bogon", "is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser")},
    }
    result.update(changes)
    return result


def node(exits, index=1):
    return Node(Endpoint(f"192.0.2.{index}"), Geo("JP", "日本", 0), proxy_exit_ips=exits)


class RiskParsingTests(unittest.TestCase):
    def test_cfnb_formula_and_all_level_boundaries(self):
        for value, level in [
            (0, LEVELS[0]),
            (0.249, LEVELS[0]),
            (0.25, LEVELS[1]),
            (4.999, LEVELS[1]),
            (5, LEVELS[2]),
            (20, LEVELS[3]),
            (100, LEVELS[4]),
        ]:
            self.assertEqual(risk_level(value), level)
        self.assertEqual(parse_risk(payload(score="0.01 (Low)"), "198.51.100.7")["score"], 5)
        self.assertEqual(parse_risk(payload(is_vpn=True), "198.51.100.7")["score"], 15)
        self.assertEqual(parse_risk(payload(is_crawler="Googlebot"), "198.51.100.7")["score"], 15)
        self.assertEqual(
            parse_risk(payload(is_bogon=True, company=None, asn=None), "198.51.100.7")["level"], LEVELS[4]
        )

    def test_incomplete_anonymous_errors_and_mismatched_ips_are_unknown_not_clean(self):
        for data in (
            {},
            [],
            {"error": "invalid key"},
            {"ip": "198.51.100.7", "is_bogon": False},
            payload(ip="198.51.100.8"),
            payload(company=None),
            payload(asn=None),
            payload(is_proxy=None),
            payload(is_proxy="false"),
            payload(score="invalid"),
            payload(score=True),
            payload(score=float("nan")),
            payload(score=-1),
            payload(score=2),
        ):
            with self.subTest(data=data):
                self.assertEqual(parse_risk(data, "198.51.100.7")["level"], "未知")


class RiskNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS["proxyip"]["risk"])
        self.cfg.update(enabled=True, retries=0, concurrency=2)
        (self.base / "ipapi_key.txt").write_text("# local key\nTEST_SECRET_KEY\n", encoding="utf-8")
        self.requests = []
        self.status = 200
        self.override = None
        self.active = self.peak = 0
        app = web.Application()
        app.router.add_post("/", self.query)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url_patch = patch("cfoptimizer.risk.API_URL", f"http://127.0.0.1:{port}/")
        self.url_patch.start()
        self.filters = []

    async def asyncTearDown(self):
        for client in self.filters:
            await client.close()
        self.url_patch.stop()
        await self.runner.cleanup()
        self.tmp.cleanup()

    def client(self):
        client = RiskFilter(self.cfg, self.base)
        self.filters.append(client)
        return client

    async def query(self, request):
        body = await request.json()
        self.requests.append((request.query_string, body))
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            await asyncio.sleep(0.01)
            if self.override is not None:
                return web.Response(body=self.override, status=self.status)
            return web.json_response(
                payload(ip=body["q"], is_vpn=body["q"].endswith(".9")), status=self.status
            )
        finally:
            self.active -= 1

    async def test_deduplicates_shared_exits_and_keeps_key_out_of_url_report_and_cache(self):
        client = self.client()
        items = [node(["198.51.100.7"], 1), node(["198.51.100.7"], 2)]
        passed, rejected = await client.filter_all(items)
        self.assertEqual((len(passed), rejected, len(self.requests)), (2, {}, 1))
        query, body = self.requests[0]
        self.assertEqual(query, "")
        self.assertEqual(body, {"q": "198.51.100.7", "key": "TEST_SECRET_KEY"})
        self.assertNotIn("TEST_SECRET_KEY", json.dumps([item.to_dict() for item in items]))
        self.assertNotIn("TEST_SECRET_KEY", client.path.read_text(encoding="utf-8"))

    async def test_cached_risks_work_without_key_and_threshold_changes_apply_immediately(self):
        first = self.client()
        await first.filter_all([node(["198.51.100.9"])])
        await first.close()
        (self.base / "ipapi_key.txt").unlink()
        self.cfg["max_level"] = "纯净"
        second = self.client()
        passed, rejected = await second.filter_all([node(["198.51.100.9"])])
        self.assertFalse(passed)
        self.assertIn("超过允许上限", next(iter(rejected.values())))
        self.assertEqual((second.stats["cache_hits"], len(self.requests)), (1, 1))

    async def test_every_exit_must_pass_and_ip_v6_is_queried_without_truncation(self):
        self.cfg["max_level"] = "纯净"
        client = self.client()
        item = node(["2001:db8::7", "198.51.100.9"])
        passed, rejected = await client.filter_all([item])
        self.assertFalse(passed)
        self.assertEqual(len(item.proxy_risks), 2)
        self.assertEqual({entry[1]["q"] for entry in self.requests}, set(item.proxy_exit_ips))
        self.assertIn("出口风险", next(iter(rejected.values())))

    async def test_disabled_persistent_cache_skips_disk_reads_and_writes_but_deduplicates_this_run(self):
        client = self.client()
        await client.filter_all([node(["198.51.100.7"])])
        before = client.path.read_bytes()
        self.cfg["cache_enabled"] = False
        with patch("cfoptimizer.risk.Path.read_text", side_effect=AssertionError("no cache read")):
            second = self.client()
        await second.filter_all([node(["198.51.100.7"]), node(["198.51.100.7"], 2)])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(client.path.read_bytes(), before)

    async def test_expired_future_nonfinite_and_corrupt_cache_entries_are_not_reused(self):
        first = self.client()
        await first.filter_all([node(["198.51.100.7"])])
        raw = json.loads(first.path.read_text())
        entry = raw["entries"]["198.51.100.7"]
        for key, value in [
            ("score", True),
            ("score", -1),
            ("score", float("inf")),
            ("updated_at", time.time() + 1000),
            ("updated_at", time.time() - 169 * 3600),
        ]:
            old = entry[key]
            entry[key] = value
            first.path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertFalse(self.client().entries)
            entry[key] = old

    async def test_unknown_policy_is_explicit_and_unknown_responses_are_not_cached_to_disk(self):
        self.override = b'{"ip":"198.51.100.7","is_proxy":false}'
        client = self.client()
        item = node(["198.51.100.7"])
        passed, rejected = await client.filter_all([item])
        self.assertFalse(passed)
        self.assertIn("未知", next(iter(rejected.values())))
        self.assertFalse(client.path.exists())
        self.cfg["unknown_policy"] = "keep"
        passed, rejected = await self.client().filter_all([item])
        self.assertEqual((len(passed), rejected), (1, {}))
        self.assertEqual(item.proxy_risks["198.51.100.7"]["level"], "未知")

    async def test_rate_limit_stops_more_queries_instead_of_retrying_entire_pool(self):
        self.cfg.update(concurrency=1, retries=2)
        self.status = 429
        passed, rejected = await self.client().filter_all([node([f"198.51.100.{i}"], i) for i in (1, 2, 3)])
        self.assertEqual((len(passed), len(rejected), len(self.requests)), (0, 3, 1))
        self.assertTrue(all("429" in reason for reason in rejected.values()))

    async def test_concurrency_bounds_unique_exit_queries(self):
        client = self.client()
        await client.filter_all([node([f"198.51.100.{i}"], i) for i in range(1, 7)])
        self.assertEqual(self.peak, 2)

    async def test_invalid_json_and_large_body_are_unknown(self):
        for body in (b"<html>error</html>", b"x" * 65537):
            self.override = body
            result = await self.client().lookup("198.51.100.7")
            self.assertEqual(result["level"], "未知")

    async def test_missing_key_is_actionable_and_does_not_fall_back_to_anonymous_queries(self):
        (self.base / "ipapi_key.txt").unlink()
        with self.assertRaisesRegex(OptimizerError, "ipapi_key.txt"):
            await self.client().filter_all([node(["198.51.100.7"])])
        self.assertEqual(self.requests, [])
