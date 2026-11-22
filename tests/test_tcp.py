import asyncio
import copy
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cfoptimizer.config import DEFAULTS, validate
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.tcp import connect, verify, verify_all
from cfoptimizer.util import OptimizerError


def make_node(ip="192.0.2.1", port=443):
    return Node(Endpoint(ip, port), Geo("JP", "日本", 0))


class TCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tcp_open_port_and_refused_port_with_no_http(self):
        connection_seen = asyncio.Event()
        closed = asyncio.Event()
        bodies = []

        async def handler(reader, writer):
            connection_seen.set()
            try:
                bodies.append(await reader.read())
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            node = make_node("127.0.0.1", port)
            result, reason = await verify(node, DEFAULTS["tcp"])
            self.assertIs(result, node, reason)
            self.assertGreater(node.tcp_latency_ms, 0)
            self.assertEqual((node.tcp_successes, node.tcp_attempts), (1, 1))
            await asyncio.wait_for(connection_seen.wait(), 2)
            await asyncio.wait_for(closed.wait(), 2)
            self.assertEqual(bodies, [b""])
        finally:
            server.close()
            await server.wait_closed()
        result, reason = await verify(make_node("127.0.0.1", port), DEFAULTS["tcp"])
        self.assertIsNone(result)
        self.assertIn("TCP 无成功连接", reason)

    async def test_rounds_success_rate_and_worst_latency_are_measured_independently(self):
        cfg = dict(DEFAULTS["tcp"], rounds=3, min_success_rate=2 / 3)
        node = make_node()
        with patch("cfoptimizer.tcp.connect", AsyncMock(side_effect=[10, OSError(), 30])) as probe:
            result, reason = await verify(node, cfg)
        self.assertIs(result, node, reason)
        self.assertEqual(probe.await_count, 3)
        self.assertEqual((node.tcp_successes, node.tcp_attempts), (2, 3))
        self.assertEqual((node.tcp_latency_ms, node.tcp_jitter_ms), (30, 10))
        cfg["min_success_rate"] = 1
        with patch("cfoptimizer.tcp.connect", AsyncMock(side_effect=[10, TimeoutError(), 30])):
            result, reason = await verify(make_node(), cfg)
        self.assertIsNone(result)
        self.assertIn("成功率不足", reason)
        cfg["min_success_rate"] = 0
        with patch("cfoptimizer.tcp.connect", AsyncMock(side_effect=TimeoutError())):
            self.assertIsNone((await verify(make_node(), cfg))[0])

    async def test_concurrency_is_bounded_and_all_candidates_are_tested(self):
        active = peak = 0
        called = []

        async def probe(endpoint, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            called.append(endpoint)
            await asyncio.sleep(0)
            active -= 1
            return 10

        nodes = [make_node(f"192.0.2.{i}") for i in range(1, 8)]
        with patch("cfoptimizer.tcp.connect", probe):
            passed, rejected = await verify_all(nodes, dict(DEFAULTS["tcp"], concurrency=2))
        self.assertEqual(passed, nodes)
        self.assertFalse(rejected)
        self.assertEqual(peak, 2)
        self.assertEqual(called, [node.endpoint for node in nodes])

    async def test_cancellation_closes_pending_socket(self):
        pending = asyncio.Event()
        task_sockets = []
        real_socket = socket.socket

        def tracking_socket(*args, **kwargs):
            connection = real_socket(*args, **kwargs)
            task_sockets.append(connection)
            return connection

        async def never_connect(connection, address):
            pending.set()
            await asyncio.Future()

        loop = asyncio.get_running_loop()
        socket_api = SimpleNamespace(
            socket=tracking_socket,
            AF_INET=socket.AF_INET,
            AF_INET6=socket.AF_INET6,
            SOCK_STREAM=socket.SOCK_STREAM,
        )
        with (
            patch("cfoptimizer.tcp.socket", socket_api),
            patch.object(loop, "sock_connect", never_connect),
        ):
            task = asyncio.create_task(connect(Endpoint("192.0.2.1"), 10))
            await asyncio.wait_for(pending.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task_sockets)
        self.assertTrue(all(connection.fileno() == -1 for connection in task_sockets))


class NewConfigTests(unittest.TestCase):
    def test_new_switches_counts_and_rates_are_validated(self):
        base = copy.deepcopy(DEFAULTS)
        base["zone"]["domain"] = "zone.invalid"
        base["sources"] = ["ips.txt"]
        self.assertEqual(validate(copy.deepcopy(base))["tcp"]["concurrency"], 300)
        cases = [
            (None, "regional_mode", "false"),
            (None, "top_n", 0),
            (None, "top_n", 1.5),
            ("zone", "cache_enabled", 0),
            ("speed", "enabled", "false"),
            ("speed", "concurrency", 0),
            ("speed", "concurrency", 1.5),
            ("speed", "connect_timeout_seconds", 0),
            ("tcp", "rounds", 0),
            ("tcp", "rounds", 1.5),
            ("tcp", "concurrency", True),
            ("tcp", "timeout_seconds", float("inf")),
            ("tcp", "min_success_rate", True),
            ("tcp", "min_success_rate", -1),
            ("tcp", "min_success_rate", 1.01),
            ("tcp", "min_success_rate", float("nan")),
        ]
        for section, key, value in cases:
            cfg = copy.deepcopy(base)
            (cfg[section] if section else cfg)[key] = value
            with self.subTest(section=section, key=key, value=value), self.assertRaises(OptimizerError):
                validate(cfg)
