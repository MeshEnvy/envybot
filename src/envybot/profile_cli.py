"""Build MeshCore CLI strings for profile book edits (same text as fleet apply)."""

from __future__ import annotations

import time
from typing import Any

from envybot.apply import (
    _profile_advert_intervals,
    desired_agc_reset_interval,
    desired_dutycycle,
    desired_fem_rxgain,
    desired_hop_retry,
    desired_hop_retry_ms,
    desired_ota_autofetch,
    desired_path_hash_mode,
    desired_powersaving,
    desired_repeat,
    owner_info_for_apply,
    resolve_guest_password,
    rxgain_apply_enabled,
)
from envybot.fleet_worker import APPLY_FIELD_ORDER, IDENTITY_PUSH_ADVERT_FIELDS
from envybot.keys_doc import UnknownPerson, plan_acl_ops, resolve_node_acl
from envybot.passwords import normalize_password, password_is_strong
from envybot.public_advert import owner_info_cli_payload
from envybot.position import site_binding
from envybot.position import public_radio_name
from envybot.public_advert import resolve_public_apply_position
from envybot.radio import airtime_factor_for_dutycycle, format_book_coord, firmware_has_dutycycle_cli

# Profile UI row ids that never enqueue radio work.
_PROFILE_BOOK_ONLY = frozenset({"paused", "acl"})


def profile_patch_keys_to_apply_fields(keys: frozenset[str] | set[str]) -> frozenset[str]:
    """Map PATCH profile keys to apply field names (ordered subset of APPLY_FIELD_ORDER)."""
    fields: set[str] = set()
    for key in keys:
        if key in _PROFILE_BOOK_ONLY:
            continue
        if key in ("trust_admin", "trust_guest"):
            fields.add("acl")
            continue
        if key == "owner_info_default":
            fields.add("owner")
            continue
        if key in APPLY_FIELD_ORDER:
            fields.add(key)
    return frozenset(fields)


def _ordered_fields(fields: frozenset[str]) -> list[str]:
    return [f for f in APPLY_FIELD_ORDER if f in fields]


def cli_commands_for_apply_field(
    field: str,
    *,
    unit_key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    firmware_version: str | None = None,
) -> list[str]:
    """One apply field → CLI command line(s). Empty when nothing to send."""
    bind = site_binding(unit_key, node, sites)
    cmds: list[str] = []

    if field == "name":
        name = public_radio_name(unit_key, node, sites, doc=doc)
        cmds.append(f"set name {name}")
    elif field == "lat":
        if bind:
            pos = resolve_public_apply_position(node, sites, key=unit_key, doc=doc)
            val = float(pos["lat"]) if pos else 0.0
        else:
            val = 0.0
        cmds.append(f"set lat {format_book_coord(val)}")
    elif field == "lon":
        if bind:
            pos = resolve_public_apply_position(node, sites, key=unit_key, doc=doc)
            val = float(pos["lon"]) if pos else 0.0
        else:
            val = 0.0
        cmds.append(f"set lon {format_book_coord(val)}")
    elif field == "advert":
        advert_interval, _ = _profile_advert_intervals(node, site_bound=bind is not None)
        if advert_interval is not None:
            cmds.append(f"set advert.interval {advert_interval}")
    elif field == "flood":
        _, flood_interval = _profile_advert_intervals(node, site_bound=bind is not None)
        if flood_interval is not None:
            cmds.append(f"set flood.advert.interval {flood_interval}")
    elif field == "guest":
        guest = resolve_guest_password(node, doc or {}, unit_key)
        cmds.append(f"set guest.password {guest}")
    elif field == "owner":
        owner_text = owner_info_for_apply(node, doc, key=unit_key, sites=sites)
        owner_cmd = owner_info_cli_payload(owner_text)
        cmds.append(f"set owner.info {owner_cmd}")
    elif field == "repeat":
        repeat_on = desired_repeat(node, sites, key=unit_key)
        cmds.append(f"set repeat {'on' if repeat_on else 'off'}")
    elif field == "admin":
        admin = node.get("admin_password")
        if password_is_strong(admin):
            cmds.append(f"password {normalize_password(admin)}")
    elif field == "path_hash":
        want = desired_path_hash_mode(node)
        cmds.append(f"set path.hash.mode {want}")
    elif field == "dutycycle":
        want = int(round(float(desired_dutycycle(node))))
        fw = firmware_version or node.get("firmware_version")
        if firmware_has_dutycycle_cli(fw) is not False:
            cmds.append(f"set dutycycle {want}")
        else:
            af = airtime_factor_for_dutycycle(float(want))
            cmds.append(f"set af {af:g}")
    elif field == "ota_autofetch":
        mode = desired_ota_autofetch(node)
        cmds.append(f"ota config autofetch {mode}")
    elif field == "powersaving":
        enabled = desired_powersaving(node)
        if enabled is not None:
            cmds.append(f"powersaving {'on' if enabled else 'off'}")
    elif field == "hop_retry":
        count = desired_hop_retry(node)
        if count is not None:
            cmds.append(f"set hop.retry {int(count)}")
    elif field == "hop_retry_ms":
        ms = desired_hop_retry_ms(node)
        if ms is not None:
            cmds.append(f"set hop.retry.ms {int(ms)}")
    elif field == "fem_rxgain":
        enabled = desired_fem_rxgain(node)
        cmds.append(f"set radio.fem.rxgain {'on' if enabled else 'off'}")
    elif field == "agc_reset_interval":
        interval = desired_agc_reset_interval(node)
        cmds.append(f"set agc.reset.interval {interval}")
    elif field == "rxgain":
        enabled = rxgain_apply_enabled(node)
        if enabled is not None:
            cmds.append(f"set radio.rxgain {'on' if enabled else 'off'}")
    elif field == "acl":
        try:
            want = resolve_node_acl(doc or {}, node, keys or {})
        except UnknownPerson:
            want = []
        for op in plan_acl_ops(want, None):
            cmds.append(f"setperm {op.key} {op.perm}")
    return cmds


def cli_commands_for_apply_fields(
    fields: frozenset[str],
    *,
    unit_key: str,
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    doc: dict[str, Any] | None,
    keys: dict[str, list[str]] | None,
    firmware_version: str | None = None,
) -> list[str]:
    """Ordered CLI lines for a profile edit, plus clock / flood advert follow-ups."""
    ordered = _ordered_fields(fields)
    if not ordered:
        return []
    out: list[str] = []
    for field in ordered:
        out.extend(
            cli_commands_for_apply_field(
                field,
                unit_key=unit_key,
                node=node,
                sites=sites,
                doc=doc,
                keys=keys,
                firmware_version=firmware_version,
            )
        )
    if ordered:
        out.append(f"time {int(time.time()) + 1}")
    if fields & IDENTITY_PUSH_ADVERT_FIELDS:
        out.append("advert")
    return out
