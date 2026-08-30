/** @param {number | null | undefined} ts */
export function formatRelative(ts) {
  if (ts == null) return 'never'
  const age = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (age < 60) return `${age}s ago`
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${(age / 3600).toFixed(1)}h ago`
  return `${(age / 86400).toFixed(1)}d ago`
}

/** @param {number | null | undefined} mv */
export function formatBattery(mv) {
  if (mv == null) return '—'
  return `${mv} mV (${(mv / 1000).toFixed(2)} V)`
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
