/** @param {number | null | undefined} ts */
export function formatRelative(ts) {
  if (ts == null) return 'never'
  const age = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (age < 60) return `${age}s ago`
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${(age / 3600).toFixed(1)}h ago`
  return `${(age / 86400).toFixed(1)}d ago`
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

/** @param {number | null | undefined} recvErrors @param {number | null | undefined} packetsRecv */
export function formatErrorRate(recvErrors, packetsRecv) {
  const errs = recvErrors ?? 0
  const recv = packetsRecv ?? 0
  if (recv === 0) return errs === 0 ? '0' : '—'
  const pct = (errs / recv) * 100
  if (pct === 0) return '0'
  if (pct < 0.01) return '<0.01%'
  if (pct < 10) return `${pct.toFixed(2)}%`
  return `${pct.toFixed(1)}%`
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
    'err_events',
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
 * Map / list label: site name when bound, else unit id.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitLabel(unit) {
  const siteName = unit?.site_name
  if (typeof siteName === 'string' && siteName) return siteName
  const label = unit?.label
  if (typeof label === 'string' && label) return label
  return String(unit?.unit_id || unit?.key || '')
}

/**
 * Detail heading: site name when bound, else unit id + radio name.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitTitle(unit) {
  const book = typeof unit?.name === 'string' ? unit.name : ''
  const siteName = typeof unit?.site_name === 'string' ? unit.site_name : ''
  if (book && siteName) return `${book} @ ${siteName}`
  if (siteName) return siteName
  if (book) return book
  return String(unit?.unit_id || unit?.key || '')
}
