"""Password doctrine: unique and strong. No radio."""

from __future__ import annotations

import unittest

from envybot.passwords import (
    assign_guest_password,
    guest_needs_assign,
    guest_profile_token,
    password_collides,
    password_is_strong,
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
        self.assertTrue(guest_needs_assign(doc["nodes"]["me0002"], doc, "me0002"))

    def test_guest_matches_own_admin(self) -> None:
        node = {"admin_password": "SamePassword12!", "guest_password": "SamePassword12!"}
        doc = {"nodes": {"me0001": node}}
        self.assertTrue(guest_needs_assign(node, doc, "me0001"))

    def test_unique_guest_ok(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "GuestOneStrong1"}
        doc = {"nodes": {"me0001": node}}
        self.assertFalse(guest_needs_assign(node, doc, "me0001"))


class AssignTests(unittest.TestCase):
    def test_rolls_shared_default(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "m35h3nvy"}
        doc = {"nodes": {"me0001": node}}
        pw = assign_guest_password(node, doc, "me0001")
        self.assertTrue(password_is_strong(pw))
        self.assertNotEqual(pw, "m35h3nvy")
        self.assertEqual(node["guest_password"], pw)
        self.assertIsInstance(node["last_guest_roll"], int)

    def test_keeps_unique_strong(self) -> None:
        node = {"admin_password": "AdminOneStrong1", "guest_password": "GuestOneStrong1"}
        doc = {"nodes": {"me0001": node}}
        self.assertEqual(assign_guest_password(node, doc, "me0001"), "GuestOneStrong1")
        self.assertNotIn("last_guest_roll", node)

    def test_profile_token_none_when_weak(self) -> None:
        self.assertEqual(guest_profile_token({"guest_password": "m35h3nvy"}), "none")
        self.assertEqual(guest_profile_token({}), "none")
        token = guest_profile_token({"guest_password": "GuestOneStrong1"})
        self.assertEqual(len(token), 8)
        self.assertNotEqual(token, "none")


if __name__ == "__main__":
    unittest.main()
