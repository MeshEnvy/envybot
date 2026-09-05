"""Routing policy resolution and live route snapshots."""

from __future__ import annotations

import unittest

from envybot.routing import (
    RouteSession,
    RoutingMode,
    has_cached_route,
    live_route_from_audit_path,
    live_route_from_contact,
    resolve_routing,
    routing_explicit,
)


class ResolveRoutingTests(unittest.TestCase):
    def test_default_path(self) -> None:
        self.assertEqual(resolve_routing({}), RoutingMode.PATH)
        self.assertEqual(resolve_routing({"unit_id": "ME0001"}), RoutingMode.PATH)
        self.assertIsNone(routing_explicit({}))

    def test_explicit_direct(self) -> None:
        node = {"routing": "direct"}
        self.assertEqual(resolve_routing(node), RoutingMode.DIRECT)
        self.assertEqual(routing_explicit(node), "direct")

    def test_explicit_flood(self) -> None:
        node = {"routing": "flood"}
        self.assertEqual(resolve_routing(node), RoutingMode.FLOOD)
        self.assertEqual(routing_explicit(node), "flood")

    def test_resolve_does_not_read_flood_key(self) -> None:
        self.assertEqual(resolve_routing({"flood": True}), RoutingMode.PATH)
        self.assertIsNone(routing_explicit({"flood": True}))


class CachedRouteTests(unittest.TestCase):
    def test_zero_hop_is_cached(self) -> None:
        self.assertTrue(
            has_cached_route({"out_path_len": 0, "out_path_hash_mode": 0, "out_path": ""})
        )

    def test_multi_hop_is_cached(self) -> None:
        self.assertTrue(has_cached_route({"out_path_len": 2, "out_path": "aabbccdd"}))

    def test_flood_not_cached(self) -> None:
        self.assertFalse(has_cached_route({"out_path_len": -1}))
        self.assertFalse(has_cached_route(None))


class LiveRouteTests(unittest.TestCase):
    def test_path_policy_flood_fallback(self) -> None:
        live = live_route_from_contact(None, policy=RoutingMode.PATH)
        self.assertEqual(live["kind"], "flood")
        self.assertTrue(live["fallback"])

    def test_path_policy_direct_live(self) -> None:
        contact = {"out_path_len": 0, "out_path_hash_mode": 0, "out_path": ""}
        live = live_route_from_contact(contact, policy=RoutingMode.PATH)
        self.assertEqual(live["kind"], "direct")
        self.assertFalse(live["fallback"])

    def test_policy_flood_not_fallback(self) -> None:
        live = live_route_from_contact(None, policy=RoutingMode.FLOOD)
        self.assertEqual(live["kind"], "flood")
        self.assertFalse(live["fallback"])

    def test_audit_path_hops(self) -> None:
        live = live_route_from_audit_path("514e fe3b", policy=RoutingMode.PATH)
        self.assertIsNotNone(live)
        assert live is not None
        self.assertEqual(live["kind"], "hops")
        self.assertEqual(live["label"], "514e fe3b")


class RouteSessionTests(unittest.TestCase):
    def test_discard_after_three(self) -> None:
        rs = RouteSession()
        self.assertFalse(rs.record_timeout(had_cached_route=True))
        self.assertFalse(rs.record_timeout(had_cached_route=True))
        self.assertTrue(rs.record_timeout(had_cached_route=True))

    def test_success_resets(self) -> None:
        rs = RouteSession(path_failures=2)
        rs.record_success()
        self.assertEqual(rs.path_failures, 0)


if __name__ == "__main__":
    unittest.main()
