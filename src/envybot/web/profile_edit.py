"""Apply fleet UI profile edits to nodes.yaml / sites.yaml."""

from __future__ import annotations

from typing import Any

from envybot.full_sync import FULL_SYNC_INTERVAL_CHOICES
from envybot.position import site_binding, write_sites_doc, load_sites_doc
from envybot.public_advert import strip_name_suffix_decorations


def _set_trust_list(node: dict[str, Any], role: str, names: Any) -> None:
    trust = node.get("trust")
    if not isinstance(trust, dict):
        trust = {}
        node["trust"] = trust
    if names is None or names == []:
        trust.pop(role, None)
        if not trust:
            node.pop("trust", None)
        return
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    if not isinstance(names, list):
        return
    cleaned = [str(n).strip().lower() for n in names if str(n).strip()]
    if cleaned:
        trust[role] = cleaned
    else:
        trust.pop(role, None)


def apply_profile_field_edit(
    key: str,
    node: dict[str, Any],
    doc: dict[str, Any],
    sites_doc: dict[str, Any],
    sites: dict[str, dict[str, Any]],
    field: str,
    value: Any,
) -> str | None:
    """Mutate book for one profile row. Returns error message or None."""
    bind = site_binding(key, node, sites)
    if field == "owner_info_default":
        if value:
            node.pop("owner_info", None)
        return None
    if field == "owner":
        if bind is None:
            return "owner applies only when site-bound"
        if value is None or (isinstance(value, str) and not value.strip()):
            node.pop("owner_info", None)
        elif isinstance(value, str):
            node["owner_info"] = value
        else:
            return "owner must be a string"
        return None
    if field == "name":
        if bind:
            slug, site = bind
            if not isinstance(value, str) or not value.strip():
                return "name required"
            site["advert_name"] = strip_name_suffix_decorations(value.strip())
            sites[slug] = site
        else:
            if value is None or value == "":
                node.pop("name", None)
            elif isinstance(value, str):
                node["name"] = value.strip()
            else:
                return "name must be a string"
        return None
    if field == "lat":
        if bind:
            slug, site = bind
            try:
                lat = float(value)
                loc = site.get("loc")
                if not isinstance(loc, list) or len(loc) < 2:
                    site["loc"] = [lat, 0.0]
                else:
                    site["loc"] = [lat, float(loc[1])]
                sites[slug] = site
            except (TypeError, ValueError):
                return "invalid latitude"
        else:
            return "GPS edit for unbound units uses loc on the node (not implemented here)"
        return None
    if field == "lon":
        if bind:
            slug, site = bind
            try:
                lon = float(value)
                loc = site.get("loc")
                if not isinstance(loc, list) or len(loc) < 2:
                    site["loc"] = [0.0, lon]
                else:
                    site["loc"] = [float(loc[0]), lon]
                sites[slug] = site
            except (TypeError, ValueError):
                return "invalid longitude"
        else:
            return "GPS edit for unbound units uses loc on the node"
        return None
    if field == "trust_admin":
        _set_trust_list(node, "admin", value)
        return None
    if field == "trust_guest":
        _set_trust_list(node, "guest", value)
        return None
    if field == "admin":
        if value is None or value == "":
            return "admin password required"
        node["admin_password"] = str(value)
        return None
    if field == "guest":
        if value is None:
            node["guest_password"] = None
        else:
            node["guest_password"] = str(value)
        return None
    if field == "full_sync_interval":
        if value is None or value == "":
            node.pop("full_sync_interval", None)
            return None
        label = str(value).strip().lower()
        if label not in FULL_SYNC_INTERVAL_CHOICES:
            return f"interval must be one of {', '.join(FULL_SYNC_INTERVAL_CHOICES)}"
        node["full_sync_interval"] = label
        return None

    yaml_key = {
        "advert": "advert_interval_min",
        "flood": "flood_advert_interval_h",
        "path_hash": "path_hash_mode",
    }.get(field, field)

    if field in ("repeat", "powersaving", "fem_rxgain", "rxgain"):
        if value is True:
            node[yaml_key] = True
        elif value is False:
            node[yaml_key] = False
        else:
            node.pop(yaml_key, None)
        return None
    if field in ("hop_retry", "hop_retry_ms", "agc_reset_interval", "dutycycle", "advert", "flood", "path_hash"):
        if value is None or value == "":
            node.pop(yaml_key, None)
        else:
            try:
                node[yaml_key] = int(value)
            except (TypeError, ValueError):
                return f"invalid {field}"
        return None
    if field == "ota_autofetch":
        if value is None or value == "":
            node.pop("ota_autofetch", None)
        else:
            node["ota_autofetch"] = str(value).strip().lower()
        return None
    return f"unknown profile field {field}"


def apply_profile_patch_body(
    key: str,
    node: dict[str, Any],
    doc: dict[str, Any],
    sites_path: Any,
    body: dict[str, Any],
) -> str | None:
    sites_doc = load_sites_doc(sites_path)
    sites = sites_doc.get("sites") or {}
    if "profile" in body and isinstance(body["profile"], dict):
        for fld, val in body["profile"].items():
            if fld == "trust_admin":
                err = apply_profile_field_edit(key, node, doc, sites_doc, sites, "trust_admin", val)
            elif fld == "trust_guest":
                err = apply_profile_field_edit(key, node, doc, sites_doc, sites, "trust_guest", val)
            else:
                err = apply_profile_field_edit(key, node, doc, sites_doc, sites, fld, val)
            if err:
                return err
    if "profile_field" in body and "profile_value" in body:
        err = apply_profile_field_edit(
            key,
            node,
            doc,
            sites_doc,
            sites,
            str(body["profile_field"]),
            body.get("profile_value"),
        )
        if err:
            return err
    if "owner_info_default" in body and body["owner_info_default"]:
        node.pop("owner_info", None)
    if body.get("trust_inherit") is True:
        node.pop("trust", None)
    write_sites_doc(sites_path, sites_doc)
    return None
