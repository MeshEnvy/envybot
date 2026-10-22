# `envybot trust`

Write site name + resolved location + pubkey onto a companion as
contacts/favorites. Applies `channels.yaml` group-channel grants. With a
person arg, also record this tag in `keys.yaml` and update `nodes.yaml`
trust roles.

```
./envybot [--book DIR] trust [person[:role] selector:role …] [flags]
```

```
./envybot trust                         # contacts + channel grants
./envybot trust ben                     # this tag is ben, fleet admin
./envybot trust bill:guest              # this tag is bill, fleet guest
./envybot trust ben me0016:guest        # fleet admin, guest on that unit
```

Contact name is the bound site's `name` (e.g. Ophir), or `unit_id` when
the unit has no site. Includes bag/bench units. Skips `decommissioned`
rows. A stale last-heard
advert name on the same key is removed and re-added so the phone list
picks up the book name. A replaced chip (same unit or site name, new
pubkey) drops the old companion contact. Bare `Repeater` names are left
alone. Reconnect the MeshCore app after trust.

Does not copy admin/guest passwords or field-node secret keys. Does not
push ACL to repeaters over the mesh; run [`fleet`](fleet.md) apply from a
tag that is already on the repeater ACL.

## Group channels

`channels.yaml` lives next to `keys.yaml` in the private book. Each channel
is defined once with a `people:` grant (`everyone` or a list of person
slugs).

**Private** channels use a mapping key plus `key:` (32-hex PSK). **Hashtag**
channels use a quoted YAML key starting with `#` (for example `"#MeshEnvy":`)
and no `key:`; trust derives the 16-byte secret as the first 16 bytes of
SHA-256 of that name (MeshCore `#topic` convention). A `public` row is
ignored; stock Public on slot 0 stays firmware-owned.

On every companion connect, `trust` GETs channel slots and SETs any granted
channel that is missing or has the wrong key. Add/update only: personal
channels already on the tag are not removed. Without `channels.yaml`, channel
work is skipped.

Person for grants: the `trust` person arg, else lookup of the live companion
pubkey in `keys.yaml`, else `everyone` channels only. Guest vs admin does
not change channel grants.

## People and roles

`keys.yaml` maps a person to one or more 64-hex companion pubkeys. Book
`trust.admin` / `trust.guest` name those people. A node `trust:` key
replaces that role for that unit (including `[]` to lock it down).

Guest-only registration updates the book only. Fleet apply (`setperm`) from
an admin tag pushes guest and admin keys onto each repeater. Admin
registration adds this companion to `keys.yaml` and `trust.admin`; the next
fleet apply on each unit SETs the resolved ACL when the stamp is stale.

`admin1_pubkey` is Meshtastic. Trust ignores it.

## Companion connection

On a TTY, `trust` always lists probed BLE/USB companions and prompts
`Pick companion [1-N]:`, even when only one tag is visible. Pass
`--companion`, `--ble ADDRESS`, `--serial PORT`, or `--tcp host:port` to
skip the picker (required for non-interactive runs). Explicit `--serial` /
`--ble MAC` / `--tcp` connect without a list when the transport is fully
specified.

## Flags

Same companion transport flags as `fleet`.

| Flag | Meaning |
|------|---------|
| `--export FILE` | Write a JSON contact list |
| `--export-only` | Write `--export` and exit (no radio) |
