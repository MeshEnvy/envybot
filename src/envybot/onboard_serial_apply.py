"""USB SET helpers aligned with fleet ``profile_parts``."""

from __future__ import annotations

import re
import time
from typing import Any

from envybot.apply import (
    applicable_field_desireds,
    board_allows_rxgain,
    desired_agc_reset_interval,
    desired_dutycycle,
    desired_fem_rxgain,
    desired_hop_retry,
    desired_hop_retry_ms,
    desired_ota_autofetch,
    desired_path_hash_mode,
    desired_powersaving,
    desired_rxgain,
)
from envybot.keys_doc import UnknownPerson, parse_serial_acl, plan_acl_ops, resolve_node_acl
from envybot.public_advert import owner_info_cli_payload
from envybot.radio import (
    airtime_factor_for_dutycycle,
    cli_admin_password_ok,
    cli_set_ok,
    fem_rxgain_cli_missing,
    firmware_has_dutycycle_cli,
    parse_coord,
    parse_dutycycle,
    parse_get_value,
    parse_int_get_value,
    parse_ota_autofetch,
)
from envybot.repeater_serial import CliError, RepeaterSerial, reply_failed

AGC_INTERVAL_ROUNDED_RE = re.compile(r"rounded to (\d+)", re.I)


def require_ok(step: str, reply: str) -> str:
    if reply_failed(reply):
        raise CliError(f"{step}: {reply}")
    return reply


def coords_match(got: float | None, want: float) -> bool:
    return got is not None and abs(got - want) < 1e-4


def apply_if_needed(
    cli: RepeaterSerial,
    *,
    step: str,
    already: bool,
    setter: str,
    verify,
    ok_label: str,
    force: bool,
) -> bool:
    """SET only when needed. Returns True if a SET was sent."""
    if already and not force:
        print(f"   {ok_label} (already)")
        return False
    require_ok(step, cli.cmd(setter))
    if not verify():
        raise CliError(f"{step} verify failed")
    print(f"   {ok_label}")
    return True


def apply_admin_password(cli: RepeaterSerial, admin_pw: str) -> None:
    """Always write admin. Cannot GET; yaml reuse is the value source only.

    Skipping SET when the book already had a password left replacement chips
    on the factory default (``password``). A bad mesh login is a silent timeout.
    """
    reply = cli.cmd(f"password {admin_pw}")
    if not cli_admin_password_ok(reply, admin_pw):
        raise CliError(f"password: {reply}")


def apply_path_hash_policy(cli: RepeaterSerial, node: dict | None, *, force: bool) -> bool:
    """``set path.hash.mode 1`` (2-byte). Skip if firmware has no CLI."""
    want = desired_path_hash_mode(node or {})
    raw = cli.cmd("get path.hash.mode")
    if reply_failed(raw):
        print(f"   skipped ({raw.strip()[:40]})")
        return False
    got = parse_int_get_value(raw)
    return apply_if_needed(
        cli,
        step="set path.hash.mode",
        already=got == want,
        setter=f"set path.hash.mode {want}",
        verify=lambda: parse_int_get_value(cli.cmd("get path.hash.mode")) == want,
        ok_label=f"{want} (2-byte)",
        force=force,
    )


def apply_dutycycle_policy(cli: RepeaterSerial, fw: str | None, node: dict | None, *, force: bool) -> bool:
    """``set dutycycle 50``, or ``set af 1`` on MeshCore <1.15."""
    want = int(desired_dutycycle(node or {}))
    af = airtime_factor_for_dutycycle(want)
    use_af = firmware_has_dutycycle_cli(fw) is False
    if not use_af:
        dc = parse_dutycycle(cli.cmd("get dutycycle"))
        if dc is not None or firmware_has_dutycycle_cli(fw) is True:
            return apply_if_needed(
                cli,
                step="set dutycycle",
                already=dc is not None and abs(dc - want) <= 0.5,
                setter=f"set dutycycle {want}",
                verify=lambda: (
                    (got := parse_dutycycle(cli.cmd("get dutycycle"))) is not None
                    and abs(got - want) <= 0.5
                ),
                ok_label=f"{want:g}%",
                force=force,
            )
    got_af = parse_coord(cli.cmd("get af"))
    return apply_if_needed(
        cli,
        step="set af",
        already=got_af is not None and abs(got_af - af) < 0.05,
        setter=f"set af {af:g}",
        verify=lambda: (
            (got := parse_coord(cli.cmd("get af"))) is not None
            and abs(got - af) < 0.05
        ),
        ok_label=f"{want:g}% (af {af:g})",
        force=force,
    )


def apply_ota_autofetch_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``ota config autofetch off`` (or book value). Skip if firmware has no OTA CLI."""
    want = desired_ota_autofetch(node or {})
    raw = cli.cmd("ota config")
    if reply_failed(raw):
        lower = raw.strip().lower()
        if lower.startswith("unknown"):
            print(f"   skipped ({raw.strip()[:40]})")
            return False
        raise CliError(f"ota config: {raw.strip()[:60]}")
    got = parse_ota_autofetch(raw)
    if got is None:
        print(f"   skipped (unparsed: {raw.strip()[:40]})")
        return False
    return apply_if_needed(
        cli,
        step="ota config autofetch",
        already=got == want,
        setter=f"ota config autofetch {want}",
        verify=lambda: parse_ota_autofetch(cli.cmd("ota config")) == want,
        ok_label=want,
        force=force,
    )


def apply_powersaving_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``powersaving on|off`` when the book sets it. Reply is not ``OK``."""
    want = desired_powersaving(node or {})
    if want is None:
        return False
    word = "on" if want else "off"
    raw = cli.cmd("powersaving")
    already = (raw or "").strip().lower().startswith(word)
    if already and not force:
        print(f"   {word} (already)")
        return False
    reply = cli.cmd(f"powersaving {word}")
    if not (reply or "").strip().lower().startswith(word):
        raise CliError(f"powersaving: {reply}")
    print(f"   {word}")
    return True


def apply_hop_retry_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``set hop.retry`` when the book sets it."""
    want = desired_hop_retry(node or {})
    if want is None:
        return False
    got = parse_int_get_value(cli.cmd("get hop.retry"))
    return apply_if_needed(
        cli,
        step="set hop.retry",
        already=got == want,
        setter=f"set hop.retry {want}",
        verify=lambda: parse_int_get_value(cli.cmd("get hop.retry")) == want,
        ok_label=str(want),
        force=force,
    )


def apply_hop_retry_ms_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``set hop.retry.ms`` when the book sets it."""
    want = desired_hop_retry_ms(node or {})
    if want is None:
        return False
    got = parse_int_get_value(cli.cmd("get hop.retry.ms"))
    return apply_if_needed(
        cli,
        step="set hop.retry.ms",
        already=got == want,
        setter=f"set hop.retry.ms {want}",
        verify=lambda: parse_int_get_value(cli.cmd("get hop.retry.ms")) == want,
        ok_label=f"{want} ms",
        force=force,
    )


def apply_fem_rxgain_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``set radio.fem.rxgain`` when the book sets it. Skip if unsupported."""
    want = desired_fem_rxgain(node or {})
    word = "on" if want else "off"
    raw = cli.cmd("get radio.fem.rxgain")
    if fem_rxgain_cli_missing(raw):
        print("   skipped (unsupported)")
        return False
    got = (raw or "").strip().lower()
    already = word in got
    return apply_if_needed(
        cli,
        step="set radio.fem.rxgain",
        already=already,
        setter=f"set radio.fem.rxgain {word}",
        verify=lambda: word in (cli.cmd("get radio.fem.rxgain") or "").lower(),
        ok_label=word,
        force=force,
    )


def apply_rxgain_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``set radio.rxgain`` when the book sets it. Skip if unsupported."""
    want = desired_rxgain(node or {})
    if want is None:
        return False
    if not board_allows_rxgain(node or {}):
        print("   skipped (need board: heltec-t096)")
        return False
    word = "on" if want else "off"
    raw = cli.cmd("get radio.rxgain")
    if fem_rxgain_cli_missing(raw):
        print("   skipped (unsupported)")
        return False
    got = (raw or "").strip().lower()
    already = word in got
    return apply_if_needed(
        cli,
        step="set radio.rxgain",
        already=already,
        setter=f"set radio.rxgain {word}",
        verify=lambda: word in (cli.cmd("get radio.rxgain") or "").lower(),
        ok_label=word,
        force=force,
    )


def apply_agc_reset_interval_policy(
    cli: RepeaterSerial,
    node: dict[str, Any] | None,
    *,
    force: bool,
) -> bool:
    """``set agc.reset.interval`` (fleet default 4). Skip if unsupported."""
    want = desired_agc_reset_interval(node or {})
    raw = cli.cmd("get agc.reset.interval")
    if fem_rxgain_cli_missing(raw):
        print("   skipped (unsupported)")
        return False
    got = parse_int_get_value(raw)
    if got == want and not force:
        print(f"   {want} (already)")
        return False
    set_reply = require_ok(
        "set agc.reset.interval",
        cli.cmd(f"set agc.reset.interval {want}"),
    )
    if not cli_set_ok(set_reply):
        raise CliError(f"set agc.reset.interval: {set_reply}")
    match = AGC_INTERVAL_ROUNDED_RE.search(set_reply)
    if match and int(match.group(1)) == want:
        print(f"   {want}")
        return True
    verify_timeout = max(cli.timeout, 8.0)
    for _ in range(3):
        time.sleep(0.35)
        try:
            verify_raw = cli.cmd("get agc.reset.interval", timeout=verify_timeout)
        except CliError:
            continue
        if parse_int_get_value(verify_raw) == want:
            print(f"   {want}")
            return True
    raise CliError("set agc.reset.interval verify failed")




def _apply_guest_password(
    cli: RepeaterSerial,
    guest_pw: str,
    *,
    force: bool,
) -> bool:
    device_guest = parse_get_value(cli.cmd("get guest.password"))
    if device_guest is None:
        device_guest = ""
    if device_guest == guest_pw and not force:
        print("   kept (already)")
        return False
    require_ok("set guest.password", cli.cmd(f"set guest.password {guest_pw}"))
    got_guest = parse_get_value(cli.cmd("get guest.password")) or ""
    if got_guest != guest_pw:
        raise CliError("guest.password verify failed")
    changed = device_guest != guest_pw
    print("   set" if changed else "   confirmed")
    return changed


def apply_profile_over_serial(
    cli: RepeaterSerial,
    unit: str,
    node: dict[str, Any],
    *,
    doc: dict[str, Any],
    keys: dict[str, list[str]],
    sites: dict[str, dict[str, Any]],
    admin_pw: str,
    guest_pw: str,
    admin_src: str,
    guest_src: str,
    fw: str | None,
    force: bool,
) -> tuple[bool, bool]:
    """Push book profile over USB using the same desired fields as mesh apply."""
    applicable = applicable_field_desireds(node, sites, doc=doc, keys=keys, key=unit)
    admin_changed = admin_src != "yaml"
    guest_changed = False

    print("2. fleet profile …")
    if "fem_rxgain" in applicable:
        apply_fem_rxgain_policy(cli, node, force=force)
    if "agc_reset_interval" in applicable:
        apply_agc_reset_interval_policy(cli, node, force=force)
    if "rxgain" in applicable:
        apply_rxgain_policy(cli, node, force=force)
    if "powersaving" in applicable:
        apply_powersaving_policy(cli, node, force=force)
    if "hop_retry" in applicable:
        apply_hop_retry_policy(cli, node, force=force)
    if "hop_retry_ms" in applicable:
        apply_hop_retry_ms_policy(cli, node, force=force)
    if "path_hash" in applicable:
        apply_path_hash_policy(cli, node, force=force)
    if "dutycycle" in applicable:
        apply_dutycycle_policy(cli, fw, node, force=force)
    if "ota_autofetch" in applicable:
        apply_ota_autofetch_policy(cli, node, force=force)

    if "advert" in applicable or "flood" in applicable:
        want_adv = int(float(applicable.get("advert", "0")))
        want_flood = int(float(applicable.get("flood", "0")))
        adv = parse_int_get_value(cli.cmd("get advert.interval"))
        flood = parse_int_get_value(cli.cmd("get flood.advert.interval"))
        ok = adv == want_adv and flood == want_flood
        if ok and not force:
            print(f"   advert {want_adv}m / flood {want_flood}h (already)")
        else:
            require_ok("set advert.interval", cli.cmd(f"set advert.interval {want_adv}"))
            require_ok(
                "set flood.advert.interval",
                cli.cmd(f"set flood.advert.interval {want_flood}"),
            )
            adv = parse_int_get_value(cli.cmd("get advert.interval"))
            flood = parse_int_get_value(cli.cmd("get flood.advert.interval"))
            if adv != want_adv or flood != want_flood:
                raise CliError(f"advert verify failed: local={adv} flood={flood}")
            print(f"   advert {want_adv}m / flood {want_flood}h")

    if "repeat" in applicable:
        want_on = applicable["repeat"] in ("True", "true", "1")
        repeat_word = "on" if want_on else "off"

        def _repeat_is(want: bool) -> bool:
            heard = (parse_get_value(cli.cmd("get repeat")) or "").strip().lower()
            return heard in ("on", "1", "true") if want else heard in ("off", "0", "false")

        apply_if_needed(
            cli,
            step=f"set repeat {repeat_word}",
            already=_repeat_is(want_on),
            setter=f"set repeat {repeat_word}",
            verify=lambda: _repeat_is(want_on),
            ok_label=repeat_word,
            force=force,
        )

    if "name" in applicable:
        want_name = applicable["name"]
        if not want_name:
            raise CliError("profile name empty")
        got = parse_get_value(cli.cmd("get name")) or ""
        apply_if_needed(
            cli,
            step="set name",
            already=got == want_name,
            setter=f"set name {want_name}",
            verify=lambda: (parse_get_value(cli.cmd("get name")) or "") == want_name,
            ok_label=want_name,
            force=force,
        )

    if "lat" in applicable and "lon" in applicable:
        want_lat = float(applicable["lat"])
        want_lon = float(applicable["lon"])
        lat = parse_coord(cli.cmd("get lat"))
        lon = parse_coord(cli.cmd("get lon"))
        pos_ok = coords_match(lat, want_lat) and coords_match(lon, want_lon)
        if pos_ok and not force:
            print(f"   position {want_lat}, {want_lon} (already)")
        else:
            require_ok("set lat", cli.cmd(f"set lat {want_lat}"))
            require_ok("set lon", cli.cmd(f"set lon {want_lon}"))
            lat = parse_coord(cli.cmd("get lat"))
            lon = parse_coord(cli.cmd("get lon"))
            if not (coords_match(lat, want_lat) and coords_match(lon, want_lon)):
                raise CliError(f"position verify failed: {lat}, {lon}")
            print(f"   position {want_lat}, {want_lon}")

    if "owner" in applicable:
        owner_text = applicable["owner"]
        owner_cmd = owner_info_cli_payload(owner_text)
        got = (parse_get_value(cli.cmd("get owner.info")) or "").strip()
        want_norm = owner_text.replace("\n", "|").replace("\r", "").strip()
        if got == want_norm and not force:
            print("   owner (already)")
        else:
            require_ok("set owner.info", cli.cmd(f"set owner.info {owner_cmd}"))
            print("   owner set")

    if "admin" in applicable:
        print("   admin password …")
        apply_admin_password(cli, admin_pw)
        print("   set" if admin_changed else "   confirmed")

    if "guest" in applicable:
        print("   guest password …")
        guest_changed = _apply_guest_password(cli, guest_pw, force=force)

    return admin_changed, guest_changed


def apply_serial_acl(
    cli: RepeaterSerial,
    doc: dict[str, Any],
    node: dict[str, Any],
    keys: dict[str, list[str]],
    *,
    force: bool,
) -> str:
    try:
        grants = resolve_node_acl(doc, node, keys)
    except UnknownPerson as exc:
        raise CliError(f"acl: unknown person {exc}") from exc
    if not grants:
        print("ACL skipped (no keys.yaml / trust names)")
        return "skipped"
    raw = cli.cmd("get acl")
    heard = parse_serial_acl(raw)
    ops = plan_acl_ops(grants, None if force else heard)
    if not ops:
        print(f"ACL already ({len(grants)} grant(s))")
        return "ok"
    for op in ops:
        require_ok(op.label, cli.cmd(f"setperm {op.key} {op.perm}"))
        print(f"   {op.label}")
    print(f"ACL set ({len(ops)} change(s))")
    return "ok"
