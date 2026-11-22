import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cfoptimizer.config import DEFAULTS, load_config, validate
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.pipeline import run
from cfoptimizer.selection import prepare, shortlist_tcp
from cfoptimizer.util import OptimizerError


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS["candidates"])
        self.endpoints = [Endpoint("192.0.2.1", 443), Endpoint("192.0.2.1", 8443), Endpoint("192.0.2.2", 443)]

    def test_all_mode_filters_ports_without_changing_them(self):
        self.cfg["ports"] = [443]
        selected, stats = prepare(self.endpoints, self.cfg)
        self.assertEqual(selected, [self.endpoints[0], self.endpoints[2]])
        self.assertEqual(stats["source_candidates"], 3)
        self.assertEqual(stats["before_ipinfo"], 2)
        self.assertEqual(prepare(self.endpoints, DEFAULTS["candidates"])[0], self.endpoints)

    def test_global_top_n_uses_tcp_latency_without_rewriting_ports(self):
        self.cfg.update(ports=[443, 8443], sample_size=2)
        pool, _ = prepare(self.endpoints, self.cfg)
        self.assertEqual(pool, self.endpoints)
        nodes = [
            Node(endpoint, Geo("JP", "日本", 0), tcp_latency_ms=latency)
            for endpoint, latency in zip(pool, [90, 10, 30])
        ]
        expected = [self.endpoints[1], self.endpoints[2]]
        for order in (nodes, list(reversed(nodes)), [nodes[1], nodes[0], nodes[2]]):
            selected, _ = shortlist_tcp(order, self.cfg)
            self.assertEqual([node.endpoint for node in selected], expected)
        self.cfg["sample_size"] = 10
        self.assertEqual(
            [node.endpoint for node in shortlist_tcp(nodes, self.cfg)[0]],
            [self.endpoints[1], self.endpoints[2], self.endpoints[0]],
        )
        self.assertEqual([node.endpoint for node in nodes], pool)

    def test_per_region_top_n_preserves_independent_country_budgets(self):
        nodes = [
            Node(Endpoint(f"192.0.2.{i}"), Geo(code, code, 0), tcp_latency_ms=latency)
            for i, (code, latency) in enumerate([("JP", 80), ("JP", 10), ("SG", 60), ("SG", 20)], 1)
        ]
        self.cfg.update(sample_size=1, sample_scope="per_region")
        self.assertEqual(prepare(self.endpoints, self.cfg)[0], self.endpoints)
        selected, stats = shortlist_tcp(nodes, self.cfg)
        self.assertEqual([node.endpoint.ip for node in selected], ["192.0.2.2", "192.0.2.4"])
        self.assertEqual(stats, {"JP": {"available": 2, "sampled": 1}, "SG": {"available": 2, "sampled": 1}})

    def test_zero_limit_keeps_all_passed_endpoints_sorted_by_tcp_latency(self):
        nodes = [
            Node(endpoint, Geo("JP", "日本", 0), tcp_latency_ms=latency)
            for endpoint, latency in zip(self.endpoints, [80, 10, 40])
        ]
        selected, stats = shortlist_tcp(nodes, self.cfg)
        self.assertEqual([node.tcp_latency_ms for node in selected], [10, 40, 80])
        self.assertEqual(len(selected), len(nodes))
        self.assertEqual(stats, {})
        self.assertEqual(shortlist_tcp([], self.cfg), ([], {}))

    def test_latency_ties_are_deterministic_and_success_count_never_overrides_latency(self):
        nodes = [
            Node(self.endpoints[0], Geo("JP", "日本", 0), tcp_latency_ms=50, tcp_successes=3),
            Node(self.endpoints[1], Geo("JP", "日本", 0), tcp_latency_ms=10, tcp_successes=1),
            Node(self.endpoints[2], Geo("JP", "日本", 0), tcp_latency_ms=10, tcp_successes=3),
        ]
        self.cfg["sample_size"] = 1
        for order in (nodes, list(reversed(nodes))):
            selected, _ = shortlist_tcp(order, self.cfg)
            self.assertEqual(selected[0].endpoint, self.endpoints[1])

    def test_invalid_new_options_and_cache_path_collisions_are_rejected(self):
        base = copy.deepcopy(DEFAULTS)
        base["zone"]["domain"] = "zone.invalid"
        base["sources"] = ["ips.txt"]
        cases = [
            ("candidates", "ports", [True]),
            ("candidates", "ports", [0]),
            ("candidates", "ports", [65536]),
            ("candidates", "ports", ["443"]),
            ("candidates", "ports", 443),
            ("candidates", "sample_size", True),
            ("candidates", "sample_size", -1),
            ("candidates", "sample_size", 1.5),
            ("candidates", "sample_size", "10"),
            ("candidates", "sample_scope", "unknown"),
            ("zone", "cache_ttl_hours", -1),
            ("zone", "cache_ttl_hours", float("nan")),
            ("zone", "cache_failure_ttl_hours", True),
            ("zone", "cache_failure_ttl_hours", float("inf")),
            ("zone", "cache_file", ""),
            ("zone", "cache_file", None),
        ]
        for section, key, value in cases:
            cfg = copy.deepcopy(base)
            cfg[section][key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(OptimizerError):
                validate(cfg)
        cfg = copy.deepcopy(base)
        cfg.update(region_basis="colo")
        cfg["candidates"].update(sample_size=2, sample_scope="per_region")
        with self.assertRaisesRegex(OptimizerError, "per_region"):
            validate(cfg)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (
                "config.json",
                "ip.txt",
                "ipinfo_tokens.txt",
                ".state/ipinfo_lite.json",
                ".git/cache.json",
            ):
                cfg = copy.deepcopy(base)
                cfg["zone"]["cache_file"] = value
                path.write_text(json.dumps(cfg), encoding="utf-8")
                with self.subTest(path=value), self.assertRaises(OptimizerError):
                    load_config(path)

    def test_older_configs_receive_compatible_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps({"zone": {"domain": "zone.invalid"}, "sources": ["ips.txt"]}), encoding="utf-8"
            )
            cfg = load_config(path)
        self.assertEqual(cfg["candidates"], DEFAULTS["candidates"])
        self.assertEqual(cfg["zone"]["cache_ttl_hours"], 6)

    def test_cli_override_cannot_enable_per_region_tcp_shortlist_for_colo(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "zone": {"domain": "zone.invalid"},
                        "sources": ["ips.txt"],
                        "candidates": {"sample_size": 10, "sample_scope": "per_region"},
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent.parent / "main.py"),
                    "check",
                    "--config",
                    str(path),
                    "--region-basis",
                    "colo",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("per_region", result.stderr)


class SelectionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["zone"]["domain"] = "zone.invalid"
        self.cfg["regions"] = {"JP": 1, "SG": 1}
        self.endpoints = [
            Endpoint("192.0.2.1", 443),
            Endpoint("192.0.2.2", 8443),
            Endpoint("192.0.2.3", 443),
            Endpoint("192.0.2.4", 443),
        ]
        self.locations = {
            endpoint.ip: Geo("JP" if i < 2 else "SG", "name", 0) for i, endpoint in enumerate(self.endpoints)
        }
        self.latencies = {endpoint: latency for endpoint, latency in zip(self.endpoints, [40, 5, 30, 10])}
        self.tcp_connect = AsyncMock(side_effect=lambda endpoint, timeout: self.latencies[endpoint])
        tcp_connect = patch("cfoptimizer.tcp.connect", self.tcp_connect)
        tcp_connect.start()
        self.addCleanup(tcp_connect.stop)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def check_pipeline(self, cfg, expected_before_geo, expected_before_zone, expected_before_tcp=None):
        async def calibrate(endpoints, cfg, base):
            self.assertEqual(endpoints, expected_before_geo)
            return self.locations

        async def probe(nodes, cfg, cache):
            self.assertEqual(
                [node.endpoint for node in nodes],
                sorted(expected_before_zone, key=self.latencies.__getitem__),
            )
            return nodes, {}

        async def speed(node, cfg, verify_tls, base):
            return node, "ok"

        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", calibrate),
            patch("cfoptimizer.pipeline.probe.verify_all", probe),
            patch("cfoptimizer.pipeline.speed.measure", speed),
        ):
            self.assertEqual(await run(cfg, self.base), 0)
        self.assertCountEqual(
            [call.args[0] for call in self.tcp_connect.await_args_list],
            expected_before_tcp if expected_before_tcp is not None else expected_before_geo,
        )
        return json.loads((self.base / "report.json").read_text(encoding="utf-8"))

    async def test_port_filter_runs_before_ipinfo_in_full_mode(self):
        self.cfg["candidates"]["ports"] = [443]
        expected = [self.endpoints[i] for i in (0, 2, 3)]
        report = await self.check_pipeline(self.cfg, expected, expected)
        self.assertEqual(report["candidate_pool"]["after_port_filter"], 3)

    async def test_global_top_n_runs_after_all_tcp_tests_and_before_zone(self):
        self.cfg["candidates"].update(ports=[443], sample_size=2)
        expected = [self.endpoints[3], self.endpoints[2]]
        before_geo = [self.endpoints[i] for i in (0, 2, 3)]
        report = await self.check_pipeline(self.cfg, before_geo, expected)
        self.assertEqual(report["candidate_pool"]["before_ipinfo"], 3)
        self.assertEqual(report["candidate_pool"]["before_tcp"], 3)
        self.assertEqual(report["candidate_pool"]["after_tcp"], 3)
        self.assertEqual(report["candidate_pool"]["after_tcp_selection"], 2)
        self.assertEqual(report["candidate_pool"]["before_zone"], 2)
        self.assertEqual(report["candidate_pool"]["selection_method"], "tcp_latency")

    async def test_region_top_n_runs_after_all_tcp_tests_but_before_zone(self):
        self.cfg["candidates"].update(ports=[443], sample_size=1, sample_scope="per_region")
        before_geo = [self.endpoints[i] for i in (0, 2, 3)]
        report = await self.check_pipeline(self.cfg, before_geo, [self.endpoints[0], self.endpoints[3]])
        self.assertEqual(report["candidate_pool"]["before_tcp"], 3)
        self.assertEqual(report["candidate_pool"]["before_zone"], 2)

    async def test_region_filter_runs_before_tcp_and_global_top_n(self):
        self.cfg["regions"] = {"JP": 1}
        self.cfg["candidates"].update(sample_size=1)
        report = await self.check_pipeline(
            self.cfg, self.endpoints, [self.endpoints[1]], expected_before_tcp=self.endpoints[:2]
        )
        self.assertEqual(report["candidate_pool"]["after_region_filter"], 2)
        self.assertEqual(report["tcp"]["tested"], 2)
        self.assertEqual(report["candidate_pool"]["before_zone"], 1)

    async def test_failed_tcp_endpoint_is_excluded_before_top_n(self):
        self.cfg.update(regional_mode=False, top_n=1)
        self.cfg["candidates"].update(sample_size=2)

        def connect(endpoint, timeout):
            if endpoint == self.endpoints[1]:
                raise TimeoutError()
            return self.latencies[endpoint]

        self.tcp_connect.side_effect = connect
        report = await self.check_pipeline(self.cfg, self.endpoints, [self.endpoints[3], self.endpoints[2]])
        self.assertEqual(report["tcp"], {"tested": 4, "passed": 3, "rejected": 1})
        self.assertEqual(report["candidate_pool"]["before_zone"], 2)
        self.assertEqual(report["rejected"][self.endpoints[1].address]["stage"], "tcp")

    async def test_empty_port_pool_does_not_query_ipinfo_or_overwrite_output(self):
        self.cfg["candidates"]["ports"] = [2053]
        (self.base / "ip.txt").write_text("previous", encoding="utf-8")
        with (
            patch("cfoptimizer.pipeline.sources.collect", AsyncMock(return_value=self.endpoints)),
            patch("cfoptimizer.pipeline.geo.calibrate", AsyncMock()) as calibrate,
            patch("cfoptimizer.pipeline.probe.verify_all", AsyncMock()) as probe,
        ):
            with self.assertRaisesRegex(OptimizerError, "端口过滤"):
                await run(self.cfg, self.base)
            calibrate.assert_not_called()
            probe.assert_not_called()
        self.assertEqual((self.base / "ip.txt").read_text(), "previous")
