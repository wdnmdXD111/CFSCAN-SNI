import copy
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path

from cfoptimizer.config import DEFAULTS, load_config, parse_regions, validate
from cfoptimizer.geo import country_name, parse_lite, read_cache
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.pipeline import format_node
from cfoptimizer.sources import parse_endpoint, parse_source_text, sample_network
from cfoptimizer.transport import cf_error
from cfoptimizer.util import OptimizerError, atomic_write


class CoreTests(unittest.TestCase):
    def test_config_regions_replace_defaults_not_merge_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps(
                    {"zone": {"domain": "test.invalid"}, "sources": ["ips.txt"], "regions": {"JP": 1}}
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path)["regions"], {"JP": 1})

    def test_ipv4_ipv6_ports_and_comments(self):
        self.assertEqual(parse_endpoint("1.2.3.4:8443#HK"), Endpoint("1.2.3.4", 8443))
        self.assertEqual(parse_endpoint("[2001:db8::1]:2053"), Endpoint("2001:db8::1", 2053))
        self.assertEqual(parse_endpoint("2001:db8::1"), Endpoint("2001:db8::1", 443))
        self.assertIsNone(parse_endpoint("1.2.3.4:99999"))
        self.assertIsNone(parse_endpoint("192.999.1.1"))

    def test_mixed_text_and_json(self):
        text = "# 8.8.8.8 ignored\n1.2.3.4:8443#香港\n[2001:db8::1]:443\n10.0.0.0/30\n"
        parsed = parse_source_text(text)
        self.assertEqual(len(parsed), 4)
        self.assertIn(Endpoint("10.0.0.2"), parsed)
        raw = [{"ip": "1.2.3.4", "port": 8443}, {"data": ["[2001:db8::2]:2053"]}]
        self.assertEqual(
            parse_source_text(json.dumps(raw)), {Endpoint("1.2.3.4", 8443), Endpoint("2001:db8::2", 2053)}
        )

    def test_huge_ipv6_and_ipv4_cidr_are_bounded(self):
        for network in ["2001:db8::/32", "10.0.0.0/8"]:
            sampled = sample_network(network, 32, 443)
            self.assertEqual(len(sampled), 32)
            self.assertEqual(len(set(sampled)), 32)
            self.assertEqual(sampled, sample_network(network, 32, 443))
            self.assertTrue(
                all(ipaddress.ip_address(item.ip) in ipaddress.ip_network(network) for item in sampled)
            )

    def test_lite_schema_not_old_json_schema(self):
        item = parse_lite({"country_code": "JP", "country": "Japan"}, now=100)
        self.assertEqual(item, Geo("JP", "日本", 100))
        self.assertIsNone(parse_lite({"country": "US"}))
        self.assertIsNone(parse_lite({"country_code": "US", "bogon": True}))
        self.assertEqual(country_name("SG"), "新加坡")

    def test_bad_cache_timestamp_does_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.json"
            path.write_text(json.dumps({"1.2.3.4": {"country_code": "HK", "updated_at": "bad"}}))
            self.assertEqual(read_cache(path, 168), {})

    def test_regions_are_explicit_quotas(self):
        self.assertEqual(parse_regions("hk:3,JP:2"), {"HK": 3, "JP": 2})
        self.assertEqual(parse_regions("*:2"), {"*": 2})
        for invalid in ["HK", "XX:2", "HK:0", {"US": True}, {"US": 1.5}, {"*": 2, "JP": 3}]:
            with self.assertRaises(OptimizerError):
                parse_regions(invalid)

    def test_zone_errors_in_cf_html_and_json(self):
        for body, code in [
            (b"error code: 1034", "CF 1034"),
            (b"<h1>Error <span>1034</span></h1>", "CF 1034"),
            (b'{"error code": 1020}', "CF 1020"),
            (b"Error 1016", "CF 1016"),
        ]:
            self.assertEqual(cf_error(body), code)
        self.assertEqual(cf_error(b"normal content"), "")

    def test_output_keeps_geo_and_colo_separate(self):
        node = Node(Endpoint("2001:db8::1"), Geo("US", "美国", 0), colo="SIN", latency_ms=90, speed_mbps=30)
        node.region_code = "SG"
        node.region_name = "新加坡"
        self.assertEqual(format_node(node, False), "[2001:db8::1]:443#SG 新加坡")
        self.assertIn("colo=SIN", format_node(node))
        self.assertEqual(node.to_dict()["geo"]["country_code"], "US")

    def test_atomic_output_and_config_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ip.txt"
            atomic_write(path, "1.2.3.4:443#JP 日本\n")
            self.assertEqual(path.read_bytes(), "1.2.3.4:443#JP 日本\n".encode())
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])
        cfg = copy.deepcopy(DEFAULTS)
        cfg["zone"]["domain"] = "zone.example"
        cfg["sources"] = ["candidates.txt"]
        self.assertEqual(validate(cfg)["zone"]["domain"], "zone.example")
        cfg["speed"]["streams"] = 0
        with self.assertRaises(OptimizerError):
            validate(cfg)
