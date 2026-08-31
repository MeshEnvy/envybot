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
./envybot trust ben --unit me0041       # sealed unit: record + login that box
```

Contact name is the bound site's `name` (e.g. Ophir), or `unit_id` when
the unit has no site. Includes bag/bench units. A stale last-heard
advert name is removed and re-added so the phone list picks up the
book name. Reconnect the MeshCore app after trust.

Does not copy admin/guest passwords or field-node secret keys.

## Group channels

`channels.yaml` lives next to `keys.yaml` in the private book. Each channel
is defined once (name + 32-hex PSK) with a `people:` grant (`everyone` or
a list of person slugs). `public` is reserved for the stock MeshCore Public
slot (no key required).

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

Firmware login always grants admin, so a guest-only registration does not
login. Those keys land via `fleet` apply (`setperm 1`) from an admin tag.
Admin registration password-logins pollable MeshCore units so the live
tag is `putClient`d. `--unit` scopes that mesh pass. YAML is written as
soon as SELF_INFO has a full pubkey.

`admin1_pubkey` is Meshtastic. Trust ignores it.

## Flags

Same companion transport flags as `fleet`.

| Flag | Meaning |
|------|---------|
| `--export FILE` | Write a JSON contact list |
| `--export-only` | Write `--export` and exit (no radio) |
| `--unit KEY` | Scope mesh ACL login only (repeatable). Contacts still get the full book. |
| `--force` | Password-login even if this key is already in `keys.yaml` |
