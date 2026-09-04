# `envybot weather`

Open-Meteo hourly cache for fleet poll history. Joins ambient temp, cloud,
precip, and wind to status/telemetry rows at read time (same pattern as sun).

```
./envybot [--book DIR] weather backfill [--force]
```

Run **once** after upgrade on the operator book. Stamps sqlite meta
`weather_backfill_at` so it does not repeat. `./envybot fleet` warns on
startup until backfill has run.

Cache lives in `data/fleet/history.sqlite` table `weather_hourly`, keyed by
rounded lat/lon + UTC hour. Bound site GPS comes from `sites.yaml` `loc`.

Live poll UI attaches weather on `/api/polls/{unit}` when cache has the hour.
Cache miss triggers a forecast fetch for that site window.
