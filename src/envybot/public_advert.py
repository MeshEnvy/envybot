"""Book-driven public advert config (suffix, owner, location offset)."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any

from envybot.position import EARTH_MI, haversine_miles, site_binding

MAX_RADIO_NAME = 32
MAX_OWNER_INFO = 120
DEFAULT_PUBLIC_NAME_SUFFIX = " {lora.sh}"

ADV_NAME_BRACE_RE = re.compile(r"\{[^}]*\}")
ADV_NAME_PIPE_RE = re.compile(r"\s*\|.*$")
ADV_NAME_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PublicAdvertConfig:
    name_suffix: str = ""
    location_accuracy_mi: float | None = None
    location_salt: str = ""
    owner_info: str = ""


def load_public_advert_config(doc: dict[str, Any] | None) -> PublicAdvertConfig | None:
    if not doc:
        return None
    raw = doc.get("public_advert")
    if not isinstance(raw, dict):
        return None
    suffix_raw = raw.get("name_suffix")
    if suffix_raw is None:
        suffix = DEFAULT_PUBLIC_NAME_SUFFIX
    elif isinstance(suffix_raw, str):
        suffix = suffix_raw if suffix_raw.strip() else DEFAULT_PUBLIC_NAME_SUFFIX
    else:
        suffix = str(suffix_raw)
    accuracy: float | None = None
    acc_raw = raw.get("location_accuracy_mi")
    if acc_raw is not None and acc_raw != "":
        try:
            accuracy = float(acc_raw)
        except (TypeError, ValueError):
            accuracy = None
    owner_raw = raw.get("owner_info")
    owner_info = owner_raw.strip() if isinstance(owner_raw, str) else ""
    salt_raw = raw.get("location_salt")
    location_salt = salt_raw.strip() if isinstance(salt_raw, str) else ""
    if not suffix and accuracy is None and not owner_info and not location_salt:
        return None
    return PublicAdvertConfig(
        name_suffix=suffix,
        location_accuracy_mi=accuracy,
        location_salt=location_salt,
        owner_info=owner_info,
    )


def strip_name_suffix_decorations(name: str) -> str:
    s = name.strip()
    s = ADV_NAME_BRACE_RE.sub("", s)
    s = ADV_NAME_PIPE_RE.sub("", s)
    return ADV_NAME_SPACE_RE.sub(" ", s).strip()


def lookup_advert_base_name(site: dict[str, Any] | None, slug: str) -> str:
    if isinstance(site, dict):
        for key in ("advert_name", "name"):
            raw = site.get(key)
            if isinstance(raw, str) and raw.strip():
                return strip_name_suffix_decorations(raw.strip())
    return slug


def site_advert_suffix(
    site: dict[str, Any] | None,
    config: PublicAdvertConfig | None,
) -> str:
    if isinstance(site, dict) and "advert_suffix" in site:
        raw = site.get("advert_suffix")
        if raw is None:
            return ""
        return raw if isinstance(raw, str) else str(raw)
    if config:
        return config.name_suffix
    return ""


def format_public_radio_name(
    base: str,
    suffix: str,
    *,
    max_len: int = MAX_RADIO_NAME,
) -> str:
    base = strip_name_suffix_decorations(base)
    if not suffix:
        return base[:max_len]
    max_base = max_len - len(suffix)
    if max_base < 1:
        return suffix[:max_len]
    return f"{base[:max_base]}{suffix}"


def public_advert_position(
    lat: float,
    lon: float,
    seed: str,
    accuracy_mi: float,
    *,
    salt: str,
) -> tuple[float, float]:
    """Stable offset in [0.5×, 1.0×] accuracy_mi. Not invertible without book salt."""
    if accuracy_mi <= 0 or not salt.strip():
        return lat, lon
    material = f"{seed.strip().lower()}:{salt.strip()}".encode()
    digest = hashlib.sha256(material).hexdigest()
    h1 = int(digest[:8], 16)
    h2 = int(digest[8:16], 16)
    bearing = (h1 % 360) * math.pi / 180.0
    min_dist = 0.5 * accuracy_mi
    span = max(accuracy_mi - min_dist, 0.01)
    distance = min_dist + (h2 % 10001) / 10001.0 * span
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang_dist = distance / EARTH_MI
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist)
        + math.cos(lat1) * math.sin(ang_dist) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def resolve_public_apply_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from envybot.position import resolve_book_position

    pos = resolve_book_position(node, sites, key=key)
    if not pos:
        return None
    lat, lon = float(pos["lat"]), float(pos["lon"])
    config = load_public_advert_config(doc)
    if config and config.location_accuracy_mi and config.location_accuracy_mi > 0:
        pk = str(node.get("identity_pubkey") or "").strip()
        salt = config.location_salt.strip()
        if pk and salt:
            lat, lon = public_advert_position(
                lat,
                lon,
                pk,
                config.location_accuracy_mi,
                salt=salt,
            )
    return {**pos, "lat": lat, "lon": lon}


@dataclass(frozen=True)
class ApplyPositionAudit:
    """Stake coords from the book vs coordinates pushed to the radio."""

    site: str
    stake_lat: float
    stake_lon: float
    radio_lat: float
    radio_lon: float
    offset_mi: float
    accuracy_mi: float | None


def audit_apply_position(
    node: dict[str, Any],
    sites: dict[str, dict[str, Any]] | None,
    *,
    key: str | None = None,
    doc: dict[str, Any] | None = None,
) -> ApplyPositionAudit | None:
    from envybot.position import resolve_book_position

    stake = resolve_book_position(node, sites, key=key)
    radio = resolve_public_apply_position(node, sites, key=key, doc=doc)
    if not stake or not radio:
        return None
    stake_lat = float(stake["lat"])
    stake_lon = float(stake["lon"])
    radio_lat = float(radio["lat"])
    radio_lon = float(radio["lon"])
    config = load_public_advert_config(doc)
    accuracy_mi = config.location_accuracy_mi if config else None
    return ApplyPositionAudit(
        site=str(stake.get("site") or ""),
        stake_lat=stake_lat,
        stake_lon=stake_lon,
        radio_lat=radio_lat,
        radio_lon=radio_lon,
        offset_mi=haversine_miles(stake_lat, stake_lon, radio_lat, radio_lon),
        accuracy_mi=accuracy_mi,
    )


def format_apply_position_log(audit: ApplyPositionAudit) -> str:
    acc = (
        f", book accuracy {audit.accuracy_mi:g} mi"
        if audit.accuracy_mi is not None and audit.accuracy_mi > 0
        else ", exact"
    )
    return (
        f"gps: stake {audit.stake_lat:.6f},{audit.stake_lon:.6f} "
        f"→ radio {audit.radio_lat:.6f},{audit.radio_lon:.6f} "
        f"(offset {audit.offset_mi:.2f} mi{acc}; stake not pushed)"
    )


def format_apply_name_log(
    key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
) -> str | None:
    bind = site_binding(key, node, sites)
    if not bind:
        return None
    slug, _site = bind
    radio_name = public_radio_name(key, node, sites, doc=doc)
    return f'name: radio "{radio_name}" (site {slug})'


def public_radio_name(
    key: str | None,
    node: dict[str, Any] | None,
    sites: dict[str, dict[str, Any]] | None,
    *,
    doc: dict[str, Any] | None = None,
    max_len: int = MAX_RADIO_NAME,
) -> str:
    """On-air name for public apply / trust (advert base + book suffix)."""
    unit_id = str((node or {}).get("unit_id") or (key or "").upper())
    config = load_public_advert_config(doc)
    bind = site_binding(key, node, sites)
    if bind:
        slug, site = bind
        base = lookup_advert_base_name(site, slug)
        suffix = site_advert_suffix(site, config)
        return format_public_radio_name(base, suffix, max_len=max_len)
    return unit_id[:max_len]


def owner_info_for_apply(
    node: dict[str, Any] | None,
    doc: dict[str, Any] | None,
    *,
    key: str | None = None,
    sites: dict[str, dict[str, Any]] | None = None,
) -> str:
    from envybot.position import site_binding

    if site_binding(key, node, sites) is None:
        return ""
    config = load_public_advert_config(doc)
    if not config or not config.owner_info:
        return ""
    text = config.owner_info.strip()
    if len(text) > MAX_OWNER_INFO:
        return text[:MAX_OWNER_INFO]
    return text


def owner_info_cli_payload(owner_info: str) -> str:
    """MeshCore CLI uses ``|`` as newline in ``set owner.info``."""
    return owner_info.replace("\n", "|").replace("\r", "")
