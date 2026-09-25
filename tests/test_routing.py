import unittest

from envybot.routing import (
    forced_path_from_extra,
    live_route_from_audit_path,
    live_route_from_contact,
    parse_force_path,
    parse_path_paste,
    parse_routing_value,
    resolve_routing,
    RouteSession,
    RoutingMode,
)


class RoutingPolicyTests(unittest.TestCase):
    def test_default_auto(self) -> None:
        self.assertEqual(resolve_routing({}), RoutingMode.AUTO)

    def test_explicit_auto(self) -> None:
        self.assertEqual(resolve_routing({"routing": "auto"}), RoutingMode.AUTO)

    def test_explicit_flood(self) -> None:
        self.assertEqual(resolve_routing({"routing": "flood"}), RoutingMode.FLOOD)

    def test_book_route(self) -> None:
        from envybot.routing import book_route_from_node

        forced = book_route_from_node({"route": "fe3b dd4d 3211"})
        assert forced is not None
        self.assertEqual(forced.hops, ("fe3b", "dd4d", "3211"))


class LiveRouteTests(unittest.TestCase):
    def test_auto_flood_fallback(self) -> None:
        live = live_route_from_contact(None, policy=RoutingMode.AUTO)
        self.assertEqual(live["kind"], "flood")
        self.assertTrue(live["fallback"])

    def test_hops_from_contact(self) -> None:
        contact = {"out_path_len": 2, "out_path": "fe3bdd4d", "out_path_hash_mode": 1}
        live = live_route_from_contact(contact, policy=RoutingMode.AUTO)
        self.assertEqual(live["kind"], "hops")
        self.assertEqual(live["label"], "fe3b dd4d")

    def test_audit_path(self) -> None:
        live = live_route_from_audit_path("514e fe3b", policy=RoutingMode.AUTO)
        self.assertEqual(live["kind"], "hops")
        self.assertEqual(live["label"], "514e fe3b")


class ForcedPathTests(unittest.TestCase):
    def test_roundtrip_extra(self) -> None:
        forced = parse_force_path("ea6e,e9bd,c458")
        assert forced is not None
        extra = {"forced_path": forced.to_extra()}
        restored = forced_path_from_extra(extra)
        assert restored is not None
        self.assertEqual(restored.hops, forced.hops)

    def test_bad_hop_len(self) -> None:
        with self.assertRaises(ValueError):
            parse_force_path("ea6,e9bd")


class ParsePathPasteTests(unittest.TestCase):
    _SAMPLE = """BTN-Barn-Repeater (a52f)
    → Ophir {lora.sh} (fe3b)
    → Spencer Peak {lora.sh} (dd4d)
    → Bald Mountain {lora.sh} (3211)"""

    def test_log_paste_keeps_all_four_hex_tokens(self) -> None:
        forced = parse_path_paste(self._SAMPLE)
        self.assertEqual(forced.hops, ("a52f", "fe3b", "dd4d", "3211"))

    def test_drop_prefix_kwarg_ignored(self) -> None:
        forced = parse_path_paste(self._SAMPLE, drop_prefix="3211")
        self.assertEqual(forced.hops, ("a52f", "fe3b", "dd4d", "3211"))

    def test_hash_mode_kwarg_does_not_shrink_token_len(self) -> None:
        paste = "a52f fe3b dd4d 3211 266a"
        forced = parse_path_paste(paste, hash_mode=0)
        self.assertEqual(forced.hops, ("a52f", "fe3b", "dd4d", "3211", "266a"))

    def test_markdown_fence_and_prose_ignored(self) -> None:
        blob = """id like to paste me0042 path like

```
BTN-Barn-Repeater (a52f)
    → Ophir {lora.sh} (fe3b)
    → Spencer Peak {lora.sh} (dd4d)
    → Bald Mountain {lora.sh} (3211)
```
"""
        forced = parse_path_paste(blob)
        self.assertEqual(forced.hops, ("a52f", "fe3b", "dd4d", "3211"))

    def test_comma_paste(self) -> None:
        forced = parse_path_paste("fe3b, dd4d, 3211")
        self.assertEqual(forced.hops, ("fe3b", "dd4d", "3211"))

    def test_space_separated_hex(self) -> None:
        forced = parse_path_paste("fe3b dd4d 3211 266a 82b1")
        self.assertEqual(forced.hops, ("fe3b", "dd4d", "3211", "266a", "82b1"))

    def test_paren_only_first_hop(self) -> None:
        text = """(a52f)
    → Ophir (fe3b)
    → Spencer Peak (dd4d)
    → Bald Mountain (3211)
    → Walker Lake East (266a)
    → Pilot Peak East (82b1)"""
        forced = parse_path_paste(text)
        self.assertEqual(
            forced.hops,
            ("a52f", "fe3b", "dd4d", "3211", "266a", "82b1"),
        )

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_path_paste("   ")

    def test_no_hex_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_path_paste("Ophir → Spencer Peak")


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


class ParseRoutingValueTests(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(parse_routing_value("direct"), RoutingMode.DIRECT)
