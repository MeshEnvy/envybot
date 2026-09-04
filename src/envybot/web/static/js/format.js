/** @param {number | null | undefined} secs age in seconds */
export function formatAgo(secs) {
  if (secs == null || Number.isNaN(Number(secs))) return '—'
  const age = Math.max(0, Math.floor(Number(secs)))
  if (age < 60) return `${age}s ago`
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${(age / 3600).toFixed(1)}h ago`
  return `${(age / 86400).toFixed(1)}d ago`
}

/** @param {number | null | undefined} ts @param {number} [now] unix seconds */
export function formatRelative(ts, now = Date.now() / 1000) {
  if (ts == null) return 'never'
  return formatAgo(Math.max(0, Math.floor(now - ts)))
}

/** @param {number | null | undefined} mv battery millivolts from status */
export function formatBattery(mv) {
  if (mv == null) return '—'
  return `${(mv / 1000).toFixed(2)} V`
}

/** Device telemetry is °C. Display °F. */
export function cToF(c) {
  return (Number(c) * 9) / 5 + 32
}

/** @param {number | null | undefined} c */
export function formatTemp(c) {
  if (c == null || !Number.isFinite(Number(c))) return '—'
  return `${cToF(c).toFixed(0)} °F`
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
  if (total === 0) return `${count} · 0%`
  return `${count} · ${formatPct((errs / total) * 100)}`
}

/** Compact duration: 12s, 8m9s, 16h36m, 1d17h */
export function formatPollWindow(secs) {
  if (secs == null || Number.isNaN(Number(secs))) return '—'
  const s = Math.max(0, Math.floor(Number(secs)))
  if (s < 60) return `${s}s`
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (d > 0) return h ? `${d}d${h}h` : `${d}d`
  if (h > 0) return m ? `${h}h${m}m` : `${h}h`
  return sec ? `${m}m${sec}s` : `${m}m`
}

/**
 * Stock-ticker change vs the previous sample.
 * @returns {{ text: string, dir: 'up' | 'down' } | null}
 */
export function formatStockDelta(curr, prev, digits = 2) {
  if (curr == null || prev == null) return null
  const a = Number(curr)
  const b = Number(prev)
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null
  return formatSignedDelta(a - b, digits)
}

/** @param {number | null | undefined} delta */
export function formatSignedDelta(delta, digits = 2) {
  if (delta == null) return null
  const d = Number(delta)
  if (!Number.isFinite(d)) return null
  if (Math.abs(d) < 0.5 * 10 ** -digits) return null
  return { text: `${d > 0 ? '+' : ''}${d.toFixed(digits)}`, dir: d > 0 ? 'up' : 'down' }
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

export const HEALTH_MARKS = {
  paused: { emoji: '⏸', label: 'Paused' },
  healthy: { emoji: '✅', label: 'Healthy' },
  unreachable: { emoji: '🚫', label: 'Unreachable' },
  attention: { emoji: '⚠️', label: 'Needs attention' },
}

/** @param {Record<string, unknown> | undefined} unit */
export function healthHeadline(unit) {
  const raw = unit?.health && typeof unit.health === 'object' ? unit.health.headline : null
  if (raw === 'paused' || raw === 'healthy' || raw === 'unreachable' || raw === 'attention') {
    return raw
  }
  if (unit?.paused) return 'paused'
  const state = unit?.session && typeof unit.session === 'object' ? unit.session.state : null
  if (state === 'unreachable' || unit?.freshness === 'never') return 'unreachable'
  if (hasHealthIssues(unit?.health)) return 'attention'
  return 'healthy'
}

/** @param {Record<string, unknown> | undefined} unit */
export function showHealthMark(unit) {
  return healthHeadline(unit) !== 'healthy'
}

/** @param {Record<string, unknown> | undefined} unit */
export function healthEmoji(unit) {
  const mark = HEALTH_MARKS[healthHeadline(unit)] || HEALTH_MARKS.healthy
  return mark.emoji
}

/** @param {Record<string, unknown> | undefined} unit */
export function healthMark(unit) {
  const mark = HEALTH_MARKS[healthHeadline(unit)] || HEALTH_MARKS.healthy
  return mark.label
}

/** @param {Record<string, unknown> | null | undefined} health */
export function healthTooltip(health) {
  if (!health) return 'Health unknown'
  const issues = Array.isArray(health.issues) ? health.issues : []
  if (issues.length) {
    return issues
      .map((issue) => {
        const why = `${issue.name}: ${issue.reason || issue.status}`
        return issue.fix ? `${why}\n→ ${issue.fix}` : why
      })
      .join('\n\n')
  }
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

/** @param {Record<string, unknown> | null | undefined} sun */
export function sunEmoji(sun) {
  return typeof sun?.emoji === 'string' ? sun.emoji : ''
}

/** @param {Record<string, unknown> | null | undefined} sun */
export function sunTitle(sun) {
  return typeof sun?.label === 'string' ? sun.label : ''
}

/** @param {Record<string, unknown> | null | undefined} sun */
export function sunElev(sun) {
  const elev = Number(sun?.elev)
  if (!Number.isFinite(elev)) return ''
  return `${Math.round(elev)}∠`
}

export function formatSite(slug) {
  if (!slug) return 'bag / bench'
  return slug
}

/**
 * List / map primary label: site name when bound, else alias, else unit id.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitLabel(unit) {
  const siteName = unit?.site_name
  if (typeof siteName === 'string' && siteName) return siteName
  const alias = unit?.alias
  if (typeof alias === 'string' && alias) return alias
  if (typeof unit?.label === 'string' && unit.label) return unit.label
  return String(unit?.unit_id || unit?.key || '')
}

/**
 * Detail heading: site name when bound, else unit id.
 * @param {Record<string, unknown> | undefined} unit
 */
export function unitTitle(unit) {
  return unitLabel(unit)
}

/**
 * Compact card chips for book apply prefs (stamped, not a live GET).
 * @param {Array<{ id?: string, label?: string, value?: string, state?: string }> | undefined} prefs
 */
export function prefBadges(prefs) {
  const out = []
  for (const p of prefs || []) {
    if (!p || !p.id) continue
    const due = p.state === 'due'
    const value = String(p.value || '')
    if (p.id === 'powersaving') {
      out.push({
        id: p.id,
        text: value === 'on' ? 'PS' : 'PS off',
        state: p.state || 'due',
        title: `Power saving ${value}${due ? ' (apply due)' : ''}`,
      })
    } else if (p.id === 'fem_rxgain') {
      out.push({
        id: p.id,
        text: value === 'off' ? 'LNA off' : 'LNA on',
        state: p.state || 'due',
        title: `FEM LNA ${value}${due ? ' (apply due)' : ''}`,
      })
    } else if (p.id === 'dutycycle') {
      out.push({
        id: p.id,
        text: `DC ${value}`,
        state: p.state || 'due',
        title: `Duty cycle ${value}${due ? ' (apply due)' : ''}`,
      })
    }
  }
  return out
}

/** Stable sidebar order: site name, else unit id. Numeric-aware. */
export function compareUnits(a, b) {
  const an = unitLabel(a)
  const bn = unitLabel(b)
  const cmp = an.localeCompare(bn, undefined, { numeric: true, sensitivity: 'base' })
  if (cmp !== 0) return cmp
  return String(a?.unit_id || a?.key || '').localeCompare(
    String(b?.unit_id || b?.key || ''),
    undefined,
    { numeric: true, sensitivity: 'base' }
  )
}
