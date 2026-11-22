import asyncio
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from cfoptimizer.config import DEFAULTS
from cfoptimizer.geo import LiteClient
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.probe import verify
from cfoptimizer.speed import measure, sample


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hosts = []
        self.targets = 0
        self.connections = set()
        self.routes = 0
        self.speed_auth = []
        app = web.Application()
        app.router.add_get("/", self.zone)
        app.router.add_get("/error", self.error)
        app.router.add_get("/download", self.download)
        app.router.add_get("/small", self.small)
        app.router.add_get("/html", self.html)
        app.router.add_get("/redirect", self.redirect)
        app.router.add_get("/target", self.target)
        app.router.add_get("/slow", self.slow)
        app.router.add_get("/lite/{ip}", self.lite)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.node = Node(Endpoint("127.0.0.1", self.port), Geo("JP", "日本", 0))
        self.zone_cfg = copy.deepcopy(DEFAULTS["zone"])
        self.zone_cfg.update(domain="does-not-resolve.invalid", path="/", scheme="http", rounds=2)
        self.speed_cfg = copy.deepcopy(DEFAULTS["speed"])
        self.speed_cfg.update(
            url="http://also-does-not-resolve.invalid/download",
            duration_seconds=0.3,
            max_bytes=1048576,
            repeats=1,
            streams=2,
        )

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def zone(self, request):
        self.hosts.append(request.host)
        return web.Response(text="zone ready", headers={"Server": "cloudflare", "CF-Ray": "abcdef-NRT"})

    async def error(self, request):
        response = web.StreamResponse(status=403, headers={"Server": "cloudflare", "CF-Ray": "abcdef-SIN"})
        await response.prepare(request)
        await response.write(b"<html><head><title>Testing</title></head>")
        await asyncio.sleep(0.01)
        await response.write(b"<h1>Error <span>1034</span></h1></html>")
        await response.write_eof()
        return response

    async def download(self, request):
        self.hosts.append(request.host)
        self.speed_auth.append(
            (
                request.headers.get("X-Speed-Token"),
                request.query.getall("token", []),
                request.query.get("keep"),
            )
        )
        self.routes += 1
        self.connections.add(request.transport)
        return web.Response(
            body=b"x" * 262144,
            content_type="application/octet-stream",
            headers={"X-Upstream-Mode": "origin", "X-Upstream-Landed": "upstream.example"},
        )

    async def target(self, request):
        self.targets += 1
        return web.Response(body=b"x" * 131072)

    async def small(self, request):
        return web.Response(body=b"x" * 128, content_type="application/octet-stream")

    async def html(self, request):
        return web.Response(body=b"x" * 131072, content_type="text/html")

    async def redirect(self, request):
        return web.Response(status=302, headers={"Location": "/target"})

    async def slow(self, request):
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        await response.prepare(request)
        try:
            while True:
                await response.write(b"x" * 16384)
                await asyncio.sleep(0.01)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    async def lite(self, request):
        token = request.query.get("token")
        if token == "invalid":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"ip": request.match_info["ip"], "country_code": "JP", "country": "Japan"})

    async def test_zone_uses_candidate_and_real_host(self):
        node, reason = await verify(self.node, self.zone_cfg)
        self.assertEqual(reason, "ok")
        self.assertEqual(node.colo, "NRT")
        self.assertEqual(len(self.hosts), 2)
        self.assertTrue(all(host == f"does-not-resolve.invalid:{self.port}" for host in self.hosts))

    async def test_delayed_1034_is_rejected(self):
        self.zone_cfg["path"] = "/error"
        node, reason = await verify(self.node, self.zone_cfg)
        self.assertIsNone(node)
        self.assertEqual(reason, "CF 1034")

    async def test_speed_multistream_keepalive_and_shared_cap(self):
        mbps, received, reason = await sample(self.node, self.speed_cfg, True)
        self.assertEqual(reason, "ok")
        self.assertGreater(mbps, 0)
        self.assertLessEqual(received, self.speed_cfg["max_bytes"])
        self.assertGreaterEqual(self.routes, 4)
        self.assertLess(len(self.connections), self.routes)
        self.assertTrue(all(host == f"also-does-not-resolve.invalid:{self.port}" for host in self.hosts))

    async def test_tiny_and_html_responses_do_not_become_speed(self):
        for path in ("/small", "/html"):
            self.speed_cfg["url"] = f"http://download.invalid{path}"
            mbps, received, reason = await sample(self.node, self.speed_cfg, True)
            self.assertEqual(mbps, 0)
            self.assertNotEqual(reason, "ok")

    async def test_redirect_never_escapes_pin(self):
        self.speed_cfg["url"] = "http://download.invalid/redirect"
        mbps, received, reason = await sample(self.node, self.speed_cfg, True)
        self.assertEqual(mbps, 0)
        self.assertIn("302", reason)
        self.assertEqual(self.targets, 0)

    async def test_worker_requires_origin_headers(self):
        self.speed_cfg.update(mode="worker", url="http://download.invalid/slow", duration_seconds=0.1)
        mbps, received, reason = await sample(self.node, self.speed_cfg, True)
        self.assertEqual(mbps, 0)
        self.assertIn("回源确认", reason)

    async def test_worker_password_file_uses_config_directory_and_ignores_environment(self):
        self.speed_cfg.update(mode="worker", url="http://download.invalid/download?keep=yes&token=old")
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "speed_token.txt").write_text("file-password\n", encoding="utf-8")
            with patch.object(os, "environ", dict(os.environ, SPEED_TOKEN="ignored-environment-password")):
                node, reason = await measure(self.node, self.speed_cfg, True, base)
        self.assertIsNotNone(node, reason)
        self.assertTrue(self.speed_auth)
        self.assertTrue(all(auth == ("file-password", ["file-password"], "yes") for auth in self.speed_auth))

    async def test_worker_without_password_does_not_use_environment_or_send_token(self):
        self.speed_cfg.update(mode="worker")
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(os, "environ", dict(os.environ, SPEED_TOKEN="ignored-environment-password")):
                node, reason = await measure(self.node, self.speed_cfg, True, Path(folder))
        self.assertIsNotNone(node, reason)
        self.assertTrue(self.speed_auth)
        self.assertTrue(all(auth == (None, [], None) for auth in self.speed_auth))

    async def test_chunked_stream_stops_at_deadline(self):
        self.speed_cfg.update(url="http://download.invalid/slow", duration_seconds=0.15)
        started = time.perf_counter()
        mbps, received, reason = await sample(self.node, self.speed_cfg, True)
        self.assertEqual(reason, "ok")
        self.assertGreater(received, 65536)
        self.assertGreater(mbps, 0)
        self.assertLess(time.perf_counter() - started, 1)

    async def test_lite_invalid_token_rotates_to_valid(self):
        cfg = copy.deepcopy(DEFAULTS["ipinfo"])
        cfg.update(api_base=f"http://127.0.0.1:{self.port}/lite", min_interval_seconds=0)
        async with aiohttp.ClientSession() as session:
            client = LiteClient(session, cfg, ["invalid", "valid"])
            result = await client.lookup("8.8.8.8")
        self.assertEqual(result.country_code, "JP")
        self.assertIn("invalid", client.disabled)
