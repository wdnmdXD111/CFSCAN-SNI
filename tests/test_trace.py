import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from cfoptimizer.config import DEFAULTS
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.pipeline import run
from cfoptimizer.probe import recheck_cached, verify, verify_all
from cfoptimizer.trace import MAX_TRACE_BYTES, TRACE_PATH, USER_AGENT, is_trace_path, validate_trace
from cfoptimizer.util import OptimizerError
from cfoptimizer.zone_cache import ZoneCache


def trace_body(**changes):
    fields = {
        "fl": "test",
        "h": "zone.invalid",
        "ip": "198.51.100.7",
        "uag": USER_AGENT,
        "visit_scheme": "https",
        "tls": "TLSv1.3",
        "colo": "NRT",
        "future_feature": "on",
    }
    fields.update(changes)
    return "".join(f"{key}={value}\n" for key, value in fields.items() if value is not None).encode()


class TraceTests(unittest.TestCase):
    def check(self, body, scheme="https", header_colo="NRT"):
        return validate_trace(body, "zone.invalid", 8443, scheme, USER_AGENT, header_colo)

    def test_parses_fields_and_accepts_unknown_fields_and_crlf(self):
        self.assertEqual(self.check(trace_body()), ("NRT", "ok"))
        self.assertEqual(self.check(trace_body().replace(b"\n", b"\r\n")), ("NRT", "ok"))

    def test_missing_and_empty_required_fields_are_rejected(self):
        for key in ("h", "ip", "colo", "visit_scheme", "uag", "tls"):
            for value in (None, ""):
                with self.subTest(key=key, value=value):
                    colo, reason = self.check(trace_body(**{key: value}))
                    self.assertEqual(colo, "")
                    self.assertIn(f"字段 {key}", reason)

    def test_host_matches_domain_and_optional_candidate_port(self):
        for host in ("zone.invalid", "ZONE.INVALID.", "zone.invalid:8443"):
            with self.subTest(host=host):
                self.assertEqual(self.check(trace_body(h=host)), ("NRT", "ok"))
        for host in (
            "another.invalid",
            "zone.invalid.evil",
            "zone.invalid:443",
            "zone.invalid:",
            "user@zone.invalid",
            "zone.invalid/path",
            "zone.invalid?",
            "zone.invalid#",
            " zone.invalid",
        ):
            with self.subTest(host=host):
                self.assertIn("trace h", self.check(trace_body(h=host))[1])

    def test_user_agent_must_echo_the_current_request(self):
        for ua in ("cached-agent", f"{USER_AGENT} "):
            self.assertIn("trace uag", self.check(trace_body(uag=ua))[1])

    def test_ip_is_valid_ipv4_or_ipv6_without_requiring_a_proxy_exit(self):
        for ip in ("198.51.100.7", "2001:db8::7", "127.0.0.1"):
            self.assertEqual(self.check(trace_body(ip=ip)), ("NRT", "ok"))
        for ip in ("not-an-ip", "999.1.1.1", "192.0.2.1:443", "2001:db8::1%eth0"):
            self.assertIn("trace ip", self.check(trace_body(ip=ip))[1])

    def test_scheme_and_tls_must_match_the_request(self):
        self.assertIn("visit_scheme", self.check(trace_body(visit_scheme="http"))[1])
        for tls in ("off", "none", "garbage", "TLSv"):
            self.assertIn("trace tls", self.check(trace_body(tls=tls))[1])
        self.assertEqual(self.check(trace_body(visit_scheme="http", tls="off"), "http"), ("NRT", "ok"))
        self.assertIn("trace tls", self.check(trace_body(visit_scheme="http"), "http")[1])

    def test_colo_is_valid_and_agrees_with_cf_ray(self):
        for colo in ("nrt", "NR", "NRT<script>"):
            self.assertIn("trace colo", self.check(trace_body(colo=colo))[1])
        for header_colo in ("SIN", ""):
            self.assertIn("CF-Ray", self.check(trace_body(), header_colo=header_colo)[1])

    def test_html_duplicate_fields_malformed_text_and_oversized_bodies_are_rejected(self):
        for body in (
            b"<html>colo=NRT</html>",
            trace_body() + b"h=zone.invalid\n",
            trace_body() + b"broken-line\n",
            trace_body() + b"\xff",
            b"x" * (MAX_TRACE_BYTES + 1),
        ):
            with self.subTest(body=body[:30]):
                self.assertNotEqual(self.check(body)[1], "ok")

    def test_trace_path_accepts_queries_without_replacing_custom_page_checks(self):
        self.assertTrue(is_trace_path(TRACE_PATH))
        self.assertTrue(is_trace_path(f"{TRACE_PATH}?check=1"))
        self.assertFalse(is_trace_path("/"))
        self.assertFalse(is_trace_path("/health"))


class TraceNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.worker_calls = 0
        self.status = 200
        self.body = None
        self.headers = {"Server": "cloudflare", "CF-Ray": "test-NRT"}
        app = web.Application()
        app.router.add_get(TRACE_PATH, self.trace)
        app.router.add_get("/", self.home)
        app.router.add_get("/__speed", self.download)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.cfg = copy.deepcopy(DEFAULTS["zone"])
        self.cfg.update(domain="zone.invalid", scheme="http", rounds=2)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.tmp.cleanup()

    def node(self):
        return Node(Endpoint("127.0.0.1", self.port), Geo("JP", "日本", 0))

    async def trace(self, request):
        self.requests.append((request.host, request.headers["User-Agent"]))
        body = self.body
        if body is None:
            body = trace_body(
                h=request.host,
                ip=request.remote,
                uag=request.headers["User-Agent"],
                visit_scheme="http",
                tls="off",
            )
        return web.Response(body=body, status=self.status, headers=self.headers)

    async def home(self, request):
        self.worker_calls += 1
        return web.Response(text="home")

    async def download(self, request):
        self.worker_calls += 1
        return web.Response(
            body=b"x" * 131072,
            content_type="application/octet-stream",
            headers={"X-Upstream-Mode": "origin", "X-Upstream-Landed": "upstream.invalid"},
        )

    async def test_default_trace_uses_pinned_candidate_host_and_all_rounds(self):
        node, reason = await verify(self.node(), self.cfg)
        self.assertIsNotNone(node, reason)
        self.assertEqual(node.colo, "NRT")
        self.assertEqual(self.requests, [(f"zone.invalid:{self.port}", USER_AGENT)] * 2)
        self.assertEqual(self.worker_calls, 0)

    async def test_trace_requires_200_even_if_other_statuses_are_allowed(self):
        self.headers["Location"] = f"http://zone.invalid:{self.port}/"
        for status in (204, 302, 404):
            self.status = status
            node, reason = await verify(self.node(), self.cfg)
            self.assertIsNone(node)
            self.assertIn("HTTP", reason)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.worker_calls, 0)

    async def test_trace_requires_cf_headers_even_if_page_header_check_is_disabled(self):
        self.cfg["require_cf_headers"] = False
        for headers in ({"Server": "cloudflare"}, {"Server": "other", "CF-Ray": "test-NRT"}):
            self.headers = headers
            node, reason = await verify(self.node(), self.cfg)
            self.assertIsNone(node)
            self.assertIn("响应头", reason)

    async def test_bad_trace_and_oversized_response_do_not_pass_on_headers_alone(self):
        for body in (
            b"<html>colo=NRT</html>",
            trace_body(h="another.invalid", visit_scheme="http", tls="off"),
            trace_body(visit_scheme="http", tls="off") + b"x" * MAX_TRACE_BYTES,
        ):
            self.body = body
            node, reason = await verify(self.node(), self.cfg)
            self.assertIsNone(node)
            self.assertIn("trace", reason)

    async def test_trace_still_applies_custom_body_and_allowed_colo_rules(self):
        for key, value, message in (
            ("expected_body", "my-worker-only-marker", "验证标记"),
            ("allowed_colos", ["SIN"], "allowed_colos"),
        ):
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = value
            node, reason = await verify(self.node(), cfg)
            self.assertIsNone(node)
            self.assertIn(message, reason)

    async def test_cached_trace_success_is_live_rechecked_before_use(self):
        cache = ZoneCache(self.cfg, self.base)
        passed, rejected = await verify_all([self.node()], self.cfg, cache)
        self.assertEqual((len(passed), len(rejected), len(self.requests)), (1, 0, 2))
        cache = ZoneCache(self.cfg, self.base)
        passed, rejected = await verify_all([self.node()], self.cfg, cache)
        self.assertEqual((len(passed), len(rejected), len(self.requests)), (1, 0, 2))
        self.body = trace_body(h="wrong.invalid", visit_scheme="http", tls="off")
        node, reason = await recheck_cached(passed[0], self.cfg, cache)
        self.assertIsNone(node)
        self.assertIn("trace h", reason)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(cache.stats["recheck_failed"], 1)
        cache.save(force=True)
        self.assertIsNone(ZoneCache(self.cfg, self.base).lookup(self.node())[0])

    async def test_pipeline_tests_worker_only_after_valid_trace_and_preserves_results_on_recheck_failure(
        self,
    ):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"].update(self.cfg)
        cfg["regions"] = {"JP": 1}
        cfg["speed"].update(
            mode="worker",
            url="http://zone.invalid/__speed",
            streams=1,
            repeats=1,
            max_bytes=65536,
            duration_seconds=0.1,
            candidates_per_region=1,
        )
        node = self.node()
        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=[node.endpoint])),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock(return_value={node.endpoint.ip: node.geo})),
        ):
            self.assertEqual(await run(cfg, self.base), 0)
            output = (self.base / "ip.txt").read_bytes()
            self.assertIn(b"#JP", output)
            self.assertEqual(self.worker_calls, 1)
            self.body = trace_body(h="wrong.invalid", visit_scheme="http", tls="off")
            with self.assertRaises(OptimizerError):
                await run(cfg, self.base)
        self.assertEqual(self.worker_calls, 1)
        self.assertEqual((self.base / "ip.txt").read_bytes(), output)
        report = json.loads((self.base / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["zone_cache"]["recheck_failed"], 1)
        self.assertFalse(report["output_updated"])
