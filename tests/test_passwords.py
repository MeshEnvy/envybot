"""Password doctrine: admin unique+strong; guest open by default."""

from __future__ import annotations

import unittest

from envybot.passwords import (
    desired_guest_password,
    guest_needs_assign,
    guest_profile_token,
    password_collides,
    password_is_strong,
    resolve_guest_password,
)


class StrengthTests(unittest.TestCase):
    def test_shared_fleet_default_is_weak(self) -> None:
        self.assertFalse(password_is_strong("m35h3nvy"))
        self.assertFalse(password_is_strong("m35h3nvYc00kie"))

    def test_placeholder_is_weak(self) -> None:
        self.assertFalse(password_is_strong("<mt>"))

    def test_short_is_weak(self) -> None:
        self.assertFalse(password_is_strong("Abcdef1"))

    def test_generated_shape_is_strong(self) -> None:
        self.assertTrue(password_is_strong("Abcdefghijklm1"))


class CollisionTests(unittest.TestCase):
    def test_guest_matches_other_unit(self) -> None:
        doc = {
            "nodes": {
                "me0001": {"admin_password": "AdminOneStrong1", "guest_password": "GuestOneStrong1"},
                "me0002": {"admin_password": "AdminTwoStrong2", "guest_password": "GuestOneStrong1"},
            }
        }
        self.assertTrue(
            password_collides("GuestOneStrong1", doc, "me0002", field="guest_password")
        )
        self.assertFalse(guest_needs_assign(doc["nodes"]["me0002"], doc, "me0002"))

    def test_guest_matches_own_admin(self) -> None:
        node = {"admin_password": "SamePassword12!", "guest_password": "SamePassword12!"}
        doc = {"nodes": {"me0001": node}}
        self.assertFalse(guest_needs_assign(node, doc, "me0001"))

    def test_unique_guest_ok(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "GuestOneStrong1"}
        doc = {"nodes": {"me0001": node}}
        self.assertFalse(guest_needs_assign(node, doc, "me0001"))


class ResolveTests(unittest.TestCase):
    def test_blank_explicit(self) -> None:
        node = {"guest_password": ""}
        doc = {"nodes": {"me0001": node}}
        self.assertEqual(resolve_guest_password(node, doc, "me0001"), "")
        self.assertEqual(desired_guest_password(node), "")

    def test_missing_key_defaults_blank(self) -> None:
        node: dict = {}
        doc = {"nodes": {"me0001": node}}
        self.assertEqual(desired_guest_password(node), "")
        self.assertEqual(resolve_guest_password(node, doc, "me0001"), "")

    def test_weak_resolves_blank(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "m35h3nvy"}
        doc = {"nodes": {"me0001": node}}
        self.assertEqual(resolve_guest_password(node, doc, "me0001"), "")

    def test_keeps_unique_strong_legacy(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "GuestOneStrong1"}
        doc = {"nodes": {"me0001": node}}
        self.assertEqual(resolve_guest_password(node, doc, "me0001"), "GuestOneStrong1")

    def test_profile_token_none_when_weak(self) -> None:
        self.assertEqual(guest_profile_token({"guest_password": "m35h3nvy"}), "none")
        self.assertEqual(guest_profile_token({}), "none")
        self.assertEqual(guest_profile_token({"guest_password": ""}), "none")
        token = guest_profile_token({"guest_password": "GuestOneStrong1"})
        self.assertEqual(len(token), 8)
        self.assertNotEqual(token, "none")


if __name__ == "__main__":
    unittest.main()
