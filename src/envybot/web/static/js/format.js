/** @param {number | null | undefined} secs age in seconds */
export function formatAgo(secs) {
  if (secs == null || Number.isNaN(Number(secs))) return '—'
  const age = Math.max(0, Math.floor(Number(secs)))
  if (age < 60) return `${age}s ago`
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${(age / 3600).toFixed(1)}h ago`
  return `${(age / 86400).toFixed(1)}d ago`
}

/** @param {number | null | undefined} ts */
export function formatRelative(ts) {
  if (ts == null) return 'never'
  return formatAgo(Math.max(0, Math.floor(Date.now() / 1000 - ts)))
}

/** @param {number | null | undefined} mv battery millivolts from status */
export function formatBattery(mv) {
  if (mv == null) return '—'
  return `${(mv / 1000).toFixed(2)} V`
}

/** @param {number | null | undefined} c */
export function formatTemp(c) {
  if (c == null) return '—'
  return `${c.toFixed(1)} °C`
}

/** @param {number | null | undefined} secs */
export function formatUptime(secs) {
  if (secs == null) return '—'
  if (secs < 3600) return `${secs}s`
  if (secs < 86400) return `${(secs / 3600).toFixed(1)}h`
  return `${(secs / 86400).toFixed(1)}d`
}

/** @param {number | null | undefined} n */
export function formatCount(n) {
  if (n == null) return '—'
  return String(Math.trunc(n))
}

/** @param {number} pct */
function formatPct(pct) {
  if (pct === 0) return '0'
  if (pct < 0.01) return '<0.01%'
  if (pct < 10) return `${pct.toFixed(2)}%`
  return `${pct.toFixed(1)}%`
}

/** Unreadable RF frame count and share of all receptions (since boot). */
export function formatUnreadableRf(recvErrors, packetsRecv) {
  if (recvErrors == null) return '—'
  const errs = recvErrors
  const recv = packetsRecv ?? 0
  const total = errs + recv
  const count = formatCount(errs)
  if (total === 0) return `${count} · 0% of receptions`
  return `${count} · ${formatPct((errs / total) * 100)} of receptions`
}

/** @param {number | null | undefined} secs */
export function formatPollWindow(secs) {
  if (secs == null) return '—'
  if (secs < 3600) return `${secs}s`
  if (secs < 86400) return `${(secs / 3600).toFixed(1)}h`
  return `${(secs / 86400).toFixed(1)}d`
}

/** @param {Record<string, unknown> | null | undefined} interval */
export function hasTrafficInterval(interval) {
  if (!interval) return false
  if (interval.reboot_reset) return true
  return [
    'packets_recv',
    'packets_sent',
    'recv_errors',
    'recv_flood',
    'recv_direct',
    'sent_flood',
    'sent_direct',
    'flood_dups',
    'direct_dups',
    'rx_airtime_pct',
  ].some((k) => interval[k] != null)
}

/** @param {number | null | undefined} pct */
export function formatAirtimePct(pct) {
  if (pct == null) return '—'
  return `${Number(pct).toFixed(1)}% RX`
}

/** @param {Record<string, unknown> | null | undefined} health */
export function healthTooltip(health) {
  if (!health) return 'Health unknown'
  const summary = health.summary
  if (typeof summary === 'string' && summary) return summary
  return String(health.grade || 'unknown')
}

/** @param {Record<string, unknown> | null | undefined} health */
export function hasHealthIssues(health) {
  return Array.isArray(health?.issues) && health.issues.length > 0
}

/** @param {number | null | undefined} snr */
export function formatSnr(snr) {
  if (snr == null) return '—'
  return `${Number(snr).toFixed(1)} dB`
}

/** @param {number | null | undefined} rssi */
export function formatRssi(rssi) {
  if (rssi == null) return '—'
  return `${Math.trunc(rssi)} dBm`
}

/** @param {number | null | undefined} nf */
export function formatNoiseFloor(nf) {
  if (nf == null) return '—'
  return `${Math.trunc(nf)} dBm`
}

/** @param {Record<string, unknown> | null | undefined} status */
export function hasTrafficStats(status) {
  if (!status) return false
  return [
    'packets_recv',
    'packets_sent',
    'recv_errors',
    'recv_flood',
    'recv_direct',
    'sent_flood',
    'sent_direct',
    'last_snr',
    'last_rssi',
    'noise_floor',
  ].some((k) => status[k] != null)
}

/** @param {string | null | undefined} slug */
export function formatSite(slug) {
  if (!slug) return 'bag / bench'
  return slug
}

/**
 * List / map primary label: site name when bound, else unit id.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitLabel(unit) {
  const siteName = unit?.site_name
  if (typeof siteName === 'string' && siteName) return siteName
  return String(unit?.unit_id || unit?.key || '')
}

/**
 * Detail heading: site name when bound, else unit id.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitTitle(unit) {
  return unitLabel(unit)
}
