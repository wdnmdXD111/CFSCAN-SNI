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
from cfoptimizer.selection import prepare, sample_regions
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

    def test_global_sample_is_bounded_without_duplicates_and_never_rewrites_ports(self):
        self.cfg.update(ports=[443, 8443], sample_size=2)
        for _ in range(8):
            selected, _ = prepare(self.endpoints, self.cfg)
            self.assertEqual(len(selected), 2)
            self.assertEqual(len(set(selected)), 2)
            self.assertTrue(set(selected) <= set(self.endpoints))
        self.cfg["sample_size"] = 10
        self.assertEqual(set(prepare(self.endpoints, self.cfg)[0]), set(self.endpoints))

    def test_per_region_sampling_preserves_independent_country_budgets(self):
        nodes = [
            Node(Endpoint(f"192.0.2.{i}"), Geo(code, code, 0))
            for i, code in enumerate(["JP", "JP", "SG", "SG"], 1)
        ]
        self.cfg.update(sample_size=1, sample_scope="per_region")
        self.assertEqual(prepare(self.endpoints, self.cfg)[0], self.endpoints)
        selected, stats = sample_regions(nodes, self.cfg)
        self.assertEqual(len(selected), 2)
        self.assertEqual({node.geo.country_code for node in selected}, {"JP", "SG"})
        self.assertEqual(stats, {"JP": {"available": 2, "sampled": 1}, "SG": {"available": 2, "sampled": 1}})

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

    def test_cli_override_cannot_enable_per_region_sampling_for_colo(self):
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

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def check_pipeline(self, cfg, expected_before_geo, expected_before_zone):
        async def calibrate(endpoints, cfg, base):
            self.assertEqual(endpoints, expected_before_geo)
            return self.locations

        async def probe(nodes, cfg, cache):
            self.assertEqual({node.endpoint for node in nodes}, set(expected_before_zone))
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
        return json.loads((self.base / "report.json").read_text(encoding="utf-8"))

    async def test_port_filter_runs_before_ipinfo_in_full_mode(self):
        self.cfg["candidates"]["ports"] = [443]
        expected = [self.endpoints[i] for i in (0, 2, 3)]
        report = await self.check_pipeline(self.cfg, expected, expected)
        self.assertEqual(report["candidate_pool"]["after_port_filter"], 3)

    async def test_global_sample_runs_before_ipinfo_and_zone(self):
        self.cfg["candidates"].update(ports=[443], sample_size=2)
        expected = [self.endpoints[0], self.endpoints[2]]
        with patch("cfoptimizer.selection.random.sample", side_effect=lambda values, count: values[:count]):
            report = await self.check_pipeline(self.cfg, expected, expected)
        self.assertEqual(report["candidate_pool"]["before_ipinfo"], 2)

    async def test_region_sample_runs_after_ipinfo_but_before_zone(self):
        self.cfg["candidates"].update(ports=[443], sample_size=1, sample_scope="per_region")
        before_geo = [self.endpoints[i] for i in (0, 2, 3)]
        with patch("cfoptimizer.selection.random.sample", side_effect=lambda values, count: values[:count]):
            report = await self.check_pipeline(self.cfg, before_geo, [self.endpoints[0], self.endpoints[2]])
        self.assertEqual(report["candidate_pool"]["before_zone"], 2)

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
