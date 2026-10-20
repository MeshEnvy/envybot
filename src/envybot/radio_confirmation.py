"""Book vs on-radio confirmation: SET stamps and pull/overheard values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from envybot.apply import _heard_matches_desired, field_desired_str
from envybot.profile_display import pref_display

ProfileRowState = Literal["n/a", "synced", "unconfirmed", "drift", "book"]
ConfirmSource = Literal["set", "pull"]


@dataclass(frozen=True)
class ConfirmationEvent:
    value: str
    confirmed_at: int
    source: ConfirmSource


@dataclass(frozen=True)
class OnRadio:
    confirmed: bool
    display: str = ""
    value: str = ""
    confirmed_at: int | None = None
    source: ConfirmSource | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"confirmed": self.confirmed}
        if not self.confirmed:
            out["display"] = "Not confirmed"
            return out
        out["display"] = self.display
        out["value"] = self.value
        if self.confirmed_at is not None:
            out["confirmed_at"] = self.confirmed_at
        if self.source is not None:
            out["source"] = self.source
        return out


# apply field id -> (heard value column, heard at column)
FIELD_PULL_COLUMNS: dict[str, tuple[str, str]] = {
    "name": ("name_heard", "name_at"),
    "lat": ("lat_heard", "gps_at"),
    "lon": ("lon_heard", "gps_at"),
    "advert": ("advert_interval_min", "advert_at"),
    "flood": ("flood_advert_interval_h", "flood_advert_at"),
    "repeat": ("repeat_heard", "repeat_at"),
    "path_hash": ("path_hash_heard", "path_hash_at"),
    "dutycycle": ("dutycycle_heard", "dutycycle_at"),
    "powersaving": ("powersaving_heard", "powersaving_at"),
    "hop_retry": ("hop_retry_heard", "hop_retry_at"),
    "hop_retry_ms": ("hop_retry_ms_heard", "hop_retry_ms_at"),
    "fem_rxgain": ("fem_rxgain_heard", "fem_rxgain_at"),
    "agc_reset_interval": ("agc_reset_interval_heard", "agc_reset_interval_at"),
    "rxgain": ("rxgain_heard", "rxgain_at"),
    "ota_autofetch": ("ota_autofetch_heard", "ota_autofetch_at"),
    "acl": ("", "acl_at"),
}


def _pull_value_raw(seen: dict[str, Any], field: str) -> Any | None:
    spec = FIELD_PULL_COLUMNS.get(field)
    if spec is None:
        return None
    col, at_col = spec
    if not col:
        at = seen.get(at_col)
        return "__acl__" if at is not None else None
    raw = seen.get(col)
    if raw is None or raw == "":
        return None
    at = seen.get(at_col)
    if at is None or at == "":
        return None
    return raw


def _pull_confirmed_at(seen: dict[str, Any], field: str) -> int | None:
    spec = FIELD_PULL_COLUMNS.get(field)
    if spec is None:
        return None
    _col, at_col = spec
    at = seen.get(at_col)
    if at is None or at == "":
        return None
    try:
        return int(at)
    except (TypeError, ValueError):
        return None


def normalize_pull_value(field: str, raw: Any) -> str:
    if field == "acl":
        return ""
    if field in ("repeat", "powersaving", "fem_rxgain", "rxgain"):
        text = str(raw).strip().lower()
        if text in ("true", "1", "on", "yes"):
            return "True"
        if text in ("false", "0", "off", "no"):
            return "False"
        return field_desired_str(raw)
    return field_desired_str(raw)


def confirmations_for_field(
    field: str,
    *,
    apply_desired: str | None,
    apply_at: int | None,
    seen: dict[str, Any] | None,
) -> list[ConfirmationEvent]:
    events: list[ConfirmationEvent] = []
    if apply_desired is not None and apply_at is not None:
        events.append(
            ConfirmationEvent(
                value=apply_desired,
                confirmed_at=apply_at,
                source="set",
            )
        )
    if seen:
        raw = _pull_value_raw(seen, field)
        at = _pull_confirmed_at(seen, field)
        if raw is not None and at is not None:
            if field == "acl":
                events.append(
                    ConfirmationEvent(value="", confirmed_at=at, source="pull")
                )
            else:
                events.append(
                    ConfirmationEvent(
                        value=normalize_pull_value(field, raw),
                        confirmed_at=at,
                        source="pull",
                    )
                )
    return events


def resolve_on_radio(field: str, events: list[ConfirmationEvent]) -> OnRadio:
    if not events:
        return OnRadio(confirmed=False)
    latest = max(events, key=lambda e: e.confirmed_at)
    if field == "acl":
        return OnRadio(
            confirmed=True,
            display="ACL snapshot",
            value="",
            confirmed_at=latest.confirmed_at,
            source=latest.source,
        )
    if field in ("admin", "guest"):
        display = latest.value or "Set"
        return OnRadio(
            confirmed=True,
            display=display,
            value=latest.value,
            confirmed_at=latest.confirmed_at,
            source=latest.source,
        )
    display = pref_display(field, _display_value(field, latest.value))
    return OnRadio(
        confirmed=True,
        display=display,
        value=latest.value,
        confirmed_at=latest.confirmed_at,
        source=latest.source,
    )


def _display_value(field: str, canonical: str) -> Any:
    if field in ("repeat", "powersaving", "fem_rxgain", "rxgain"):
        return canonical in ("True", "true", "1")
    if field in ("lat", "lon", "dutycycle", "hop_retry", "hop_retry_ms", "agc_reset_interval"):
        try:
            return float(canonical)
        except (TypeError, ValueError):
            return canonical
    if field in ("advert", "flood", "path_hash"):
        try:
            return int(float(canonical))
        except (TypeError, ValueError):
            return canonical
    return canonical


def password_on_radio(
    field: str,
    *,
    apply_desired: str | None,
    apply_at: int | None,
    guest_open: bool = False,
) -> OnRadio:
    if apply_desired is None or apply_at is None:
        return OnRadio(confirmed=False)
    if field == "guest" and guest_open:
        display = "Open"
    elif field == "guest" and apply_desired == "":
        display = "Open"
    else:
        display = "Set"
    return OnRadio(
        confirmed=True,
        display=display,
        value=apply_desired,
        confirmed_at=apply_at,
        source="set",
    )


def book_matches_on_radio(
    field: str,
    book_desired: str,
    on_radio: OnRadio,
    *,
    node: dict[str, Any] | None = None,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    sites: dict[str, Any] | None = None,
    unit: str | None = None,
    heard_acl: list[dict[str, Any]] | None = None,
) -> bool:
    if not on_radio.confirmed:
        return False
    if field == "acl":
        return False
    if field in ("admin", "guest"):
        return field_desired_str(book_desired) == on_radio.value
    heard_val: Any = _display_value(field, on_radio.value)
    return _heard_matches_desired(
        field,
        book_desired,
        heard_val,
        node=node or {},
        doc=doc,
        keys=keys,
        sites=sites,
        unit=unit or "",
    )


def profile_row_state(
    field: str,
    *,
    applicable: bool,
    book_desired: str | None,
    on_radio: OnRadio,
    node: dict[str, Any] | None = None,
    doc: dict[str, Any] | None = None,
    keys: dict[str, list[str]] | None = None,
    sites: dict[str, Any] | None = None,
    unit: str | None = None,
    acl_in_sync: bool | None = None,
) -> ProfileRowState:
    if not applicable:
        return "n/a"
    if field in ("admin", "guest"):
        if not on_radio.confirmed:
            return "unconfirmed"
        if book_desired is None:
            return "n/a"
        if field == "guest" and book_desired == "" and on_radio.display == "Open":
            return "synced"
        if on_radio.value == field_desired_str(book_desired):
            return "synced"
        return "drift"
    if field == "acl":
        if on_radio.confirmed and acl_in_sync is True:
            return "synced"
        if on_radio.confirmed:
            return "drift"
        return "unconfirmed"
    if not on_radio.confirmed:
        return "unconfirmed"
    if book_desired is None:
        return "n/a"
    if book_matches_on_radio(
        field,
        book_desired,
        on_radio,
        node=node,
        doc=doc,
        keys=keys,
        sites=sites,
        unit=unit,
    ):
        return "synced"
    return "drift"


def row_needs_attention(
    state: ProfileRowState,
    refresh_meta: dict[str, Any] | None,
) -> bool:
    if state not in ("unconfirmed", "drift"):
        return False
    if not refresh_meta:
        return True
    phase = str(refresh_meta.get("phase") or "")
    if phase in ("queued", "running"):
        return False
    return True


def confirmation_source_label(source: ConfirmSource | None) -> str:
    if source == "set":
        return "SET"
    if source == "pull":
        return "Pull"
    return ""
