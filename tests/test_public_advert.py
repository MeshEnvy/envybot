"""Book-driven public advert config and offset."""

from __future__ import annotations

import unittest

from envybot.apply import desired_repeat, profile_parts
from envybot.public_advert import (
    DEFAULT_PUBLIC_NAME_SUFFIX,
    MAX_ADVERT_NAME_WITH_GPS,
    audit_apply_position,
    collect_advert_name_violations,
    format_apply_name_log,
    format_apply_position_log,
    format_public_radio_name,
    load_public_advert_config,
    owner_info_cli_payload,
    owner_info_for_apply,
    public_advert_position,
    public_radio_name,
    resolve_public_apply_position,
    strip_name_suffix_decorations,
)


DOC = {
    "public_advert": {
        "name_suffix": " {lora.sh}",
        "location_accuracy_mi": 1.5,
        "location_salt": "test-book-salt-not-for-production",
        "owner_info": "MeshEnvy NCC\nhello@meshenvy.org\nLocations are accurate to 1.5 miles",
    }
}

PUBKEY = "fe3beb42dd17936680e23568ebd681768727ebd94d1ceaab071ea92adaafe283"


class PublicAdvertConfigTests(unittest.TestCase):
    def test_load_config(self) -> None:
        cfg = load_public_advert_config(DOC)
        assert cfg is not None
        self.assertEqual(cfg.name_suffix, " {lora.sh}")
        self.assertEqual(cfg.location_accuracy_mi, 1.5)
        self.assertIn("MeshEnvy NCC", cfg.owner_info)

    def test_default_suffix_when_omitted(self) -> None:
        cfg = load_public_advert_config({"public_advert": {"location_accuracy_mi": 1.5}})
        assert cfg is not None
        self.assertEqual(cfg.name_suffix, DEFAULT_PUBLIC_NAME_SUFFIX)

    def test_strip_suffix_decorations(self) -> None:
        self.assertEqual(strip_name_suffix_decorations("Ophir {meshenvy.org}"), "Ophir")
        self.assertEqual(strip_name_suffix_decorations("Foo | bar"), "Foo")

    def test_format_name_truncates_base(self) -> None:
        name = format_public_radio_name("Very Long Site Name Here", " {lora.sh}")
        self.assertLessEqual(len(name), 32)
        self.assertTrue(name.endswith("{lora.sh}"))

    def test_public_radio_name_uses_advert_name(self) -> None:
        node = {"unit_id": "ME0003", "identity_pubkey": PUBKEY}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        self.assertEqual(public_radio_name("me0003", node, sites, doc=DOC), "Ophir {lora.sh}")

    def test_offset_is_deterministic_and_in_range(self) -> None:
        lat, lon = 39.5, -119.8
        salt = DOC["public_advert"]["location_salt"]
        lat2, lon2 = public_advert_position(lat, lon, PUBKEY, 1.5, salt=salt)
        lat3, lon3 = public_advert_position(lat, lon, PUBKEY, 1.5, salt=salt)
        self.assertEqual((lat2, lon2), (lat3, lon3))
        self.assertNotEqual((lat2, lon2), (lat, lon))
        from envybot.position import haversine_miles

        dist = haversine_miles(lat, lon, lat2, lon2)
        self.assertGreaterEqual(dist, 0.75)
        self.assertLessEqual(dist, 1.5 + 0.01)

    def test_offset_requires_salt(self) -> None:
        lat, lon = 39.5, -119.8
        self.assertEqual(public_advert_position(lat, lon, PUBKEY, 1.5, salt=""), (lat, lon))

    def test_different_salt_different_offset(self) -> None:
        lat, lon = 39.5, -119.8
        a = public_advert_position(lat, lon, PUBKEY, 1.5, salt="salt-a")
        b = public_advert_position(lat, lon, PUBKEY, 1.5, salt="salt-b")
        self.assertNotEqual(a, b)

    def test_no_salt_skips_offset_in_apply(self) -> None:
        node = {"unit_id": "ME0003", "identity_pubkey": PUBKEY}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        doc = {
            "public_advert": {
                "location_accuracy_mi": 1.5,
                "owner_info": "MeshEnvy NCC",
            }
        }
        pos = resolve_public_apply_position(node, sites, key="me0003", doc=doc)
        assert pos is not None
        self.assertEqual(pos["lat"], 39.5)
        self.assertEqual(pos["lon"], -119.8)

    def test_resolve_public_apply_position_offsets(self) -> None:
        node = {"unit_id": "ME0003", "identity_pubkey": PUBKEY}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        pos = resolve_public_apply_position(node, sites, key="me0003", doc=DOC)
        assert pos is not None
        self.assertNotEqual(pos["lat"], 39.5)

    def test_owner_info_cli_payload(self) -> None:
        self.assertEqual(owner_info_cli_payload("a\nb"), "a|b")

    def test_owner_info_empty_when_unbound(self) -> None:
        node = {"unit_id": "ME0003"}
        self.assertEqual(owner_info_for_apply(node, DOC, key="me0003", sites={}), "")

    def test_owner_info_when_site_bound(self) -> None:
        node = {"unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8]}}
        self.assertIn("MeshEnvy NCC", owner_info_for_apply(node, DOC, key="me0003", sites=sites))

    def test_owner_info_node_override(self) -> None:
        node = {"unit_id": "ME0003", "owner_info": "Site-specific owner"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8]}}
        self.assertEqual(
            owner_info_for_apply(node, DOC, key="me0003", sites=sites),
            "Site-specific owner",
        )

    def test_desired_repeat_bound_default_on(self) -> None:
        node = {"unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8]}}
        self.assertTrue(desired_repeat(node, sites, key="me0003"))

    def test_desired_repeat_bench_default_off(self) -> None:
        self.assertFalse(desired_repeat({"unit_id": "ME0041"}, {}, key="me0041"))

    def test_desired_repeat_book_override(self) -> None:
        node = {"repeat": False}
        sites = {"ophir": {"node": "me0003", "loc": [39.5, -119.8]}}
        self.assertFalse(desired_repeat(node, sites, key="me0003"))

    def test_profile_parts_site_bound_with_doc(self) -> None:
        node = {
            "unit_id": "ME0003",
            "identity_pubkey": PUBKEY,
            "guest_password": "GuestOneStrong1",
            "admin_password": "AdminOneStrong1",
        }
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        parts = profile_parts(node, sites, doc=DOC, key="me0003")
        self.assertEqual(parts["name"], "Ophir {lora.sh}")
        self.assertIn("MeshEnvy NCC", parts["owner"])
        self.assertTrue(parts["repeat"])
        self.assertNotEqual(parts["lat"], 39.5)

    def test_audit_apply_position_offset(self) -> None:
        node = {"unit_id": "ME0003", "identity_pubkey": PUBKEY}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        audit = audit_apply_position(node, sites, key="me0003", doc=DOC)
        assert audit is not None
        self.assertEqual(audit.site, "ophir")
        self.assertEqual(audit.stake_lat, 39.5)
        self.assertEqual(audit.stake_lon, -119.8)
        self.assertNotEqual((audit.radio_lat, audit.radio_lon), (39.5, -119.8))
        self.assertGreaterEqual(audit.offset_mi, 0.75)
        self.assertLessEqual(audit.offset_mi, 1.5 + 0.01)
        text = format_apply_position_log(audit)
        self.assertIn("stake 39.500000,-119.800000", text)
        self.assertIn("stake not pushed", text)

    def test_audit_apply_position_exact(self) -> None:
        node = {"unit_id": "ME0003", "identity_pubkey": PUBKEY}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        doc = {"public_advert": {"location_accuracy_mi": 0}}
        audit = audit_apply_position(node, sites, key="me0003", doc=doc)
        assert audit is not None
        self.assertEqual(audit.offset_mi, 0.0)
        self.assertEqual(audit.radio_lat, 39.5)
        self.assertIn(", exact", format_apply_position_log(audit))

    def test_format_apply_name_log(self) -> None:
        node = {"unit_id": "ME0003"}
        sites = {"ophir": {"node": "me0003", "advert_name": "Ophir", "loc": [39.5, -119.8]}}
        text = format_apply_name_log("me0003", node, sites, doc=DOC)
        self.assertEqual(text, 'name: radio "Ophir {lora.sh}" (site ophir)')

    def test_collect_advert_name_violations(self) -> None:
        node = {"unit_id": "ME0048", "identity_pubkey": PUBKEY}
        sites = {
            "bare-mountain-east": {
                "node": "me0048",
                "advert_name": "Bare Mountain E",
                "loc": [36.87, -116.68],
            }
        }
        nodes = {"me0048": node}
        errors = collect_advert_name_violations(DOC, nodes, sites)
        self.assertEqual(len(errors), 1)
        self.assertIn("25 chars", errors[0])
        self.assertEqual(MAX_ADVERT_NAME_WITH_GPS, 23)


if __name__ == "__main__":
    unittest.main()
