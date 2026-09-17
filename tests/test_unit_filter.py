"""Unit tests for fleet --only / --skip spec resolution."""

from __future__ import annotations

import unittest

from envybot.unit_filter import (
    UnitFilterError,
    key_in_unit_filter,
    parse_unit_specs,
    resolve_unit_specs,
)

FAKE_PUB = "a" * 64
OTHER_PUB = "b" * 64


def sample_doc() -> dict:
    return {
        "nodes": {
            "me0040": {
                "unit_id": "ME0040",
                "identity_pubkey": FAKE_PUB,
                "admin_password": "secret-admin",
            },
            "me0048": {
                "unit_id": "ME0048",
                "identity_pubkey": "3d357475f6cac431597363820a9fc767c74bbc0cd21c583873616f38f202fc54",
                "admin_password": "other-admin",
            },
            "me0050": {
                "unit_id": "ME0050",
                "identity_pubkey": "c" * 64,
                "admin_password": "pv-admin",
            },
            "me0051": {
                "unit_id": "ME0051",
                "identity_pubkey": "d" * 64,
                "admin_password": "pv-admin2",
            },
            "me0041": {
                "unit_id": "ME0041",
                "alias": "Yuki",
                "identity_pubkey": "e" * 64,
                "admin_password": "bag-admin",
            },
        }
    }


SAMPLE_SITES = {
    "poito-peak": {"node": "me0040", "name": "Poito"},
    "pv-south": {"node": "me0050", "name": "PV South"},
}


class KeyInUnitFilterTests(unittest.TestCase):
    def test_include_only(self) -> None:
        self.assertTrue(key_in_unit_filter("me0048", include={"me0048"}, skip=None))
        self.assertFalse(key_in_unit_filter("me0049", include={"me0048"}, skip=None))

    def test_skip(self) -> None:
        self.assertFalse(key_in_unit_filter("me0048", include=None, skip={"me0048"}))

    def test_include_and_skip(self) -> None:
        self.assertFalse(
            key_in_unit_filter("me0048", include={"me0048", "me0049"}, skip={"me0048"})
        )


class ParseUnitSpecsTests(unittest.TestCase):
    def test_comma_split(self) -> None:
        self.assertEqual(parse_unit_specs(["me0051,me0050"]), ["me0051", "me0050"])

    def test_repeatable(self) -> None:
        self.assertEqual(parse_unit_specs(["me0051", "me0050"]), ["me0051", "me0050"])

    def test_strips_whitespace(self) -> None:
        self.assertEqual(parse_unit_specs([" me0051 , me0050 "]), ["me0051", "me0050"])


class ResolveUnitSpecsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = sample_doc()
        self.sites = SAMPLE_SITES

    def test_exact_key(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["me0048"])
        self.assertEqual(keys, {"me0048"})

    def test_exact_unit_id(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["ME0051"])
        self.assertEqual(keys, {"me0051"})

    def test_comma_list(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, parse_unit_specs(["me0051,me0050"]))
        self.assertEqual(keys, {"me0051", "me0050"})

    def test_glob_range(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["me00[45]*"])
        self.assertEqual(keys, {"me0040", "me0048", "me0050", "me0051", "me0041"})

    def test_glob_bracket_class(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["me005[01]"])
        self.assertEqual(keys, {"me0050", "me0051"})

    def test_site_name(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["Poito"])
        self.assertEqual(keys, {"me0040"})

    def test_site_slug(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["pv-south"])
        self.assertEqual(keys, {"me0050"})

    def test_alias(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["yuki"])
        self.assertEqual(keys, {"me0041"})

    def test_unknown_exact(self) -> None:
        with self.assertRaises(UnitFilterError) as ctx:
            resolve_unit_specs(self.doc, self.sites, ["me9999"])
        self.assertIn("unknown unit", str(ctx.exception))

    def test_glob_no_match(self) -> None:
        with self.assertRaises(UnitFilterError) as ctx:
            resolve_unit_specs(self.doc, self.sites, ["me09*"])
        self.assertIn("matched nothing", str(ctx.exception))

    def test_or_across_tokens(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["me0048", "yuki"])
        self.assertEqual(keys, {"me0048", "me0041"})

    def test_pubkey_prefix(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["3d35"])
        self.assertEqual(keys, {"me0048"})

    def test_pubkey_prefix_glob(self) -> None:
        keys = resolve_unit_specs(self.doc, self.sites, ["3d35*"])
        self.assertEqual(keys, {"me0048"})

    def test_pubkey_exact(self) -> None:
        pub = "3d357475f6cac431597363820a9fc767c74bbc0cd21c583873616f38f202fc54"
        keys = resolve_unit_specs(self.doc, self.sites, [pub])
        self.assertEqual(keys, {"me0048"})

    def test_unknown_hex_not_unit_key(self) -> None:
        with self.assertRaises(UnitFilterError) as ctx:
            resolve_unit_specs(self.doc, self.sites, ["deadbeef"])
        self.assertIn("unknown unit", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
