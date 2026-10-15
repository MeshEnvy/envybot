# export

Book CSV export for **trusted administrators** who already hold the private fleet book. Output includes unit passwords and identity secrets. Do not commit or share exported files.

## sites

One row per site in `sites.yaml`, sorted by slug. Skips sites whose bound node is **decommissioned** or **`firmware_platform: meshtastic`**. Site columns are always filled; node columns are blank when there is no bind or the key is missing.

Writes CSV to **stdout**. Row count goes to **stderr** so redirects stay clean.

```bash
export ENVYBOT_HOME=/path/to/book
./envybot export sites --tags=installed > sites.csv
```

| Flag | Meaning |
|------|---------|
| `--tags TAG` | Repeatable. Comma-separated tags in one value are split. A site is included only if its `tags` list contains **every** required tag (case-insensitive AND). Omit to export all sites. |

Does not open sqlite or talk to radios. Does not run advert-name validation (export works even when `fleet` would refuse a bad book).

### Columns

Fixed order (see `SITE_EXPORT_COLUMNS` in `src/envybot/book_export.py`):

`site`, `site_name`, `site_lat`, `site_lon`, `admin_password`, `identity_pubkey`, `height_m`, `advert_name`, `unit_id`, `board`, `firmware_version`, `bootloader_version`, `advert_interval_min`, `flood_advert_interval_h`, `path_hash_mode`, `dutycycle`, `powersaving`, `hop_retry`, `hop_retry_ms`, `fem_rxgain`, `agc_reset_interval`, `rxgain`, `guest_password`, `identity_secret`.
