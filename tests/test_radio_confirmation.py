"""Book vs on-radio confirmation merge."""

from __future__ import annotations

import unittest

from envybot.radio_confirmation import (
    confirmations_for_field,
    password_on_radio,
    profile_row_state,
    resolve_on_radio,
    row_needs_attention,
)


class RadioConfirmationTest(unittest.TestCase):
    def test_set_only(self) -> None:
        events = confirmations_for_field(
            "repeat",
            apply_desired="True",
            apply_at=100,
            seen=None,
        )
        self.assertEqual(len(events), 1)
        radio = resolve_on_radio("repeat", events)
        self.assertTrue(radio.confirmed)
        self.assertEqual(radio.display, "on")
        self.assertEqual(radio.source, "set")
        self.assertEqual(radio.confirmed_at, 100)

    def test_pull_newer_than_set(self) -> None:
        seen = {"repeat_heard": "False", "repeat_at": 200}
        events = confirmations_for_field(
            "repeat",
            apply_desired="True",
            apply_at=100,
            seen=seen,
        )
        radio = resolve_on_radio("repeat", events)
        self.assertEqual(radio.display, "off")
        self.assertEqual(radio.source, "pull")
        self.assertEqual(radio.confirmed_at, 200)

    def test_pull_older_than_set(self) -> None:
        seen = {"repeat_heard": "False", "repeat_at": 50}
        events = confirmations_for_field(
            "repeat",
            apply_desired="True",
            apply_at=100,
            seen=seen,
        )
        radio = resolve_on_radio("repeat", events)
        self.assertEqual(radio.display, "on")
        self.assertEqual(radio.source, "set")

    def test_never_confirmed(self) -> None:
        radio = resolve_on_radio("dutycycle", [])
        self.assertFalse(radio.confirmed)
        d = radio.to_dict()
        self.assertEqual(d["display"], "Not confirmed")

    def test_advert_pull(self) -> None:
        seen = {"advert_interval_min": 15, "advert_at": 300}
        events = confirmations_for_field("advert", apply_desired=None, apply_at=None, seen=seen)
        radio = resolve_on_radio("advert", events)
        self.assertTrue(radio.confirmed)
        self.assertEqual(radio.display, "15")

    def test_password_set(self) -> None:
        radio = password_on_radio("admin", apply_desired="***", apply_at=1)
        self.assertTrue(radio.confirmed)
        self.assertEqual(radio.display, "Set")

    def test_password_unconfirmed(self) -> None:
        radio = password_on_radio("admin", apply_desired=None, apply_at=None)
        self.assertFalse(radio.confirmed)

    def test_profile_state_synced(self) -> None:
        events = confirmations_for_field("repeat", apply_desired="True", apply_at=1, seen=None)
        radio = resolve_on_radio("repeat", events)
        state = profile_row_state(
            "repeat",
            applicable=True,
            book_desired="True",
            on_radio=radio,
        )
        self.assertEqual(state, "synced")

    def test_profile_state_drift(self) -> None:
        events = confirmations_for_field("repeat", apply_desired="True", apply_at=1, seen=None)
        radio = resolve_on_radio("repeat", events)
        state = profile_row_state(
            "repeat",
            applicable=True,
            book_desired="False",
            on_radio=radio,
        )
        self.assertEqual(state, "drift")

    def test_profile_state_unconfirmed(self) -> None:
        radio = resolve_on_radio("repeat", [])
        state = profile_row_state(
            "repeat",
            applicable=True,
            book_desired="True",
            on_radio=radio,
        )
        self.assertEqual(state, "unconfirmed")

    def test_attention_suppressed_when_queued(self) -> None:
        self.assertFalse(
            row_needs_attention("drift", {"phase": "queued"}),
        )
        self.assertTrue(row_needs_attention("drift", {"phase": "idle"}))

    def test_lat_lon_shared_gps_at(self) -> None:
        seen = {"lat_heard": 39.5, "lon_heard": -119.8, "gps_at": 400}
        lat_events = confirmations_for_field("lat", apply_desired=None, apply_at=None, seen=seen)
        lon_events = confirmations_for_field("lon", apply_desired=None, apply_at=None, seen=seen)
        self.assertEqual(lat_events[0].confirmed_at, 400)
        self.assertEqual(lon_events[0].confirmed_at, 400)


if __name__ == "__main__":
    unittest.main()
