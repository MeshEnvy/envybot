/** @param {Array<{ ts?: number, value?: number }>} points */
function validPoints(points) {
  return points
    .filter((p) => p.value != null && Number.isFinite(Number(p.value)))
    .map((p) => ({ ts: p.ts, value: Number(p.value), synthetic: !!p.synthetic }))
    .filter((p) => p.ts != null && Number.isFinite(Number(p.ts)))
}

/**
 * Time-proportional x, with a floor so clustered polls stay distinct.
 * @param {number[]} timestamps sorted
 */
export function layoutTimeX(timestamps, width, padding = 2, minGap = 7, domain = null) {
  const innerW = width - padding * 2
  if (!timestamps.length) return []
  const tMin = domain?.tMin ?? timestamps[0]
  const tMax = domain?.tMax ?? timestamps[timestamps.length - 1]
  if (timestamps.length === 1 && !domain) return [padding + innerW / 2]
  const span = Math.max(1, tMax - tMin)
  let xs = timestamps.map((t) => padding + ((Number(t) - tMin) / span) * innerW)
  if (!domain && minGap > 0) {
    for (let i = 1; i < xs.length; i += 1) {
      if (xs[i] < xs[i - 1] + minGap) xs[i] = xs[i - 1] + minGap
    }
    const last = xs[xs.length - 1]
    const limit = padding + innerW
    if (last > limit) {
      const origin = xs[0]
      const used = last - origin || 1
      const scale = innerW / used
      xs = xs.map((x) => padding + (x - origin) * scale)
    }
  }
  return xs.map((x) => Number(x.toFixed(1)))
}

function yRange(values, minSpan) {
  const rawMin = Math.min(...values)
  const rawMax = Math.max(...values)
  let span = rawMax - rawMin
  const floor = minSpan > 0 ? minSpan : 0
  if (span < floor) {
    const mid = (rawMin + rawMax) / 2
    return { min: mid - floor / 2, span: floor || 1 }
  }
  const pad = span * 0.15
  return { min: rawMin - pad, span: span + pad * 2 }
}

/**
 * Wall-clock sparkline over the samples' own time span.
 * @param {Array<{ ts?: number, value?: number }>} points
 * @param {{ padding?: number, minSpan?: number, minGap?: number }} [opts]
 * @returns {{ line: string | null, dots: Array<{ x: number, y: number }> } | null}
 */
export function sparklineWallTime(points, width, height, opts = {}) {
  const padding = opts.padding ?? 2
  const valid = validPoints(points)
  if (!valid.length) return null
  valid.sort((a, b) => Number(a.ts) - Number(b.ts))

  const domain =
    opts.tMin != null && opts.tMax != null ? { tMin: Number(opts.tMin), tMax: Number(opts.tMax) } : null
  const xs = layoutTimeX(
    valid.map((p) => Number(p.ts)),
    width,
    padding,
    domain ? 0 : opts.minGap ?? 7,
    domain
  )
  const values = valid.map((p) => p.value)
  const { min, span } =
    opts.yMin != null && opts.yMax != null
      ? { min: Number(opts.yMin), span: Number(opts.yMax) - Number(opts.yMin) || 1 }
      : yRange(values, opts.minSpan ?? 0)
  const innerH = height - padding * 2

  const dots = valid.map((p, i) => {
    const y = padding + innerH - ((p.value - min) / span) * innerH
    return { x: xs[i], y: Number(y.toFixed(1)), synthetic: !!p.synthetic }
  })
  const line = dots.length >= 2 ? dots.map((d) => `${d.x},${d.y}`).join(' ') : null
  const zeroY = padding + innerH - ((0 - min) / span) * innerH
  return {
    line,
    dots: opts.dots === false ? [] : dots,
    zeroY: Number(zeroY.toFixed(1)),
  }
}

function sparklineLayer(points, width, height, yMin, ySpan, opts = {}) {
  const padding = opts.padding ?? 2
  const valid = validPoints(points)
  if (!valid.length) return null
  valid.sort((a, b) => Number(a.ts) - Number(b.ts))
  const domain =
    opts.tMin != null && opts.tMax != null ? { tMin: Number(opts.tMin), tMax: Number(opts.tMax) } : null
  const xs = layoutTimeX(
    valid.map((p) => Number(p.ts)),
    width,
    padding,
    domain ? 0 : opts.minGap ?? 7,
    domain
  )
  const innerH = height - padding * 2
  const dots = valid.map((p, i) => {
    const y = padding + innerH - ((p.value - yMin) / ySpan) * innerH
    return { x: xs[i], y: Number(y.toFixed(1)), synthetic: !!p.synthetic }
  })
  const line = dots.length >= 2 ? dots.map((d) => `${d.x},${d.y}`).join(' ') : null
  return { line, dots }
}

/**
 * Two series on one wall-clock chart with a shared Y scale (e.g. internal + ambient temp).
 * @returns {{ layers: Array<{ line: string | null, dots: Array<{ x: number, y: number }>, stroke: string, dashed?: boolean }> } | null}
 */
export function sparklineWallTimeDual(primary, secondary, width, height, opts = {}) {
  const merged = [...validPoints(primary), ...validPoints(secondary)]
  if (!merged.length) return null
  const values = merged.map((p) => p.value)
  const { min, span } =
    opts.yMin != null && opts.yMax != null
      ? { min: Number(opts.yMin), span: Number(opts.yMax) - Number(opts.yMin) || 1 }
      : yRange(values, opts.minSpan ?? 0)
  const layers = []
  const primaryLayer = sparklineLayer(primary, width, height, min, span, opts)
  if (primaryLayer && (primaryLayer.line || primaryLayer.dots.length)) {
    layers.push({ ...primaryLayer, stroke: opts.strokePrimary || '#f0b86e' })
  }
  const secondaryLayer = sparklineLayer(secondary, width, height, min, span, opts)
  if (secondaryLayer && (secondaryLayer.line || secondaryLayer.dots.length)) {
    layers.push({
      ...secondaryLayer,
      stroke: opts.strokeSecondary || '#4ea1ff',
      dashed: true,
    })
  }
  if (!layers.length) return null
  return { layers }
}

/** @param {Record<string, unknown> | null | undefined} poll @param {string} field */
export function isSynthetic(poll, field) {
  const syn = poll?.synthetic
  return Array.isArray(syn) && syn.includes(field)
}

/** Voltage from poll row (battery_mv or telemetry voltage). */
export function pollVoltage(poll) {
  if (poll?.battery_mv != null && Number.isFinite(Number(poll.battery_mv))) {
    return Number(poll.battery_mv) / 1000
  }
  const volts = poll?.voltage
  if (volts != null && Number.isFinite(Number(volts))) {
    return Number(volts)
  }
  return null
}

/**
 * Spark series from the same poll snapshots as the recent-polls table.
 * Zero-delta traffic intervals hold the last rate / unreadable % (not 0).
 * @param {Array<Record<string, unknown>>} polls newest-first or mixed
 */
export function seriesFromPolls(polls) {
  const chrono = [...(polls || [])]
    .filter((p) => p && p.ts != null)
    .sort((a, b) => Number(a.ts) - Number(b.ts))

  /** @type {Record<string, Array<{ ts: number, value: number }>>} */
  const out = {
    battery_mv: [],
    temperature: [],
    ambient_temp: [],
    noise_floor: [],
    recv_rate: [],
    unreadable_pct: [],
  }
  let lastRate = null
  let lastUnreadable = null

  for (const p of chrono) {
    const ts = Number(p.ts)
    const volts = pollVoltage(p)
    if (volts != null) {
      out.battery_mv.push({ ts, value: volts, synthetic: isSynthetic(p, 'voltage') })
    }
    if (p.temperature != null && Number.isFinite(Number(p.temperature))) {
      out.temperature.push({
        ts,
        value: Number(p.temperature),
        synthetic: isSynthetic(p, 'temperature'),
      })
    }
    const ambient = p.weather?.temp_c
    if (ambient != null && Number.isFinite(Number(ambient))) {
      out.ambient_temp.push({ ts, value: Number(ambient) })
    }
    if (p.noise_floor != null && Number.isFinite(Number(p.noise_floor))) {
      out.noise_floor.push({
        ts,
        value: Number(p.noise_floor),
        synthetic: isSynthetic(p, 'noise_floor'),
      })
    }

    if (p.reboot) {
      lastRate = null
      lastUnreadable = null
      continue
    }
    const dRecv = p.delta_packets_recv
    const dErr = p.delta_recv_errors
    if (dRecv == null && dErr == null) continue
    const recv = Number(dRecv ?? 0)
    const err = Number(dErr ?? 0)
    const dur = Number(p.since_prev_secs ?? 0)
    if (recv === 0 && err === 0) {
      if (lastRate != null) out.recv_rate.push({ ts, value: lastRate })
      if (lastUnreadable != null) out.unreadable_pct.push({ ts, value: lastUnreadable })
      continue
    }
    if (dur > 0 && recv >= 0) {
      lastRate = recv / (dur / 3600)
      out.recv_rate.push({ ts, value: lastRate })
    }
    const total = recv + err
    if (total > 0) {
      lastUnreadable = (err / total) * 100
      out.unreadable_pct.push({ ts, value: lastUnreadable })
    }
  }
  return out
}

export const SPARK_MIN_SPAN = {
  battery_mv: 0.5,
  temperature: 15,
  unreadable_pct: 20,
  recv_rate: 200,
  noise_floor: 20,
  neighbor_count: 3,
}

/**
 * Neighbor count over time from neighbors poll history.
 * @param {Array<{ ts?: number, count?: number }>} rows newest-first or mixed
 * @param {number | null | undefined} liveCount append current heard count at nowSec
 * @param {number | null | undefined} nowSec
 */
export function seriesNeighborCount(rows, liveCount, nowSec) {
  const series = []
  for (const row of rows || []) {
    if (row?.ts == null || row.count == null) continue
    const n = Number(row.count)
    if (!Number.isFinite(n)) continue
    series.push({ ts: Number(row.ts), value: n })
  }
  series.sort((a, b) => a.ts - b.ts)
  if (liveCount != null && liveCount >= 0 && nowSec != null && Number.isFinite(Number(nowSec))) {
    const last = series[series.length - 1]
    if (!last || last.value !== liveCount) {
      series.push({ ts: Number(nowSec), value: liveCount })
    }
  }
  return series
}

/**
 * Spark series from orthogonal status + telemetry histories (store).
 * @param {{ status?: Array<Record<string, unknown>>, telemetry?: Array<Record<string, unknown>> }} history
 */
export function seriesFromHistories(history) {
  const status = [...(history?.status || [])].sort((a, b) => Number(a.ts) - Number(b.ts))
  const telemetry = [...(history?.telemetry || [])].sort((a, b) => Number(a.ts) - Number(b.ts))

  /** @type {Record<string, Array<{ ts: number, value: number }>>} */
  const out = {
    battery_mv: [],
    temperature: [],
    noise_floor: [],
    recv_rate: [],
    unreadable_pct: [],
    sun: [],
  }
  let lastRate = null
  let lastUnreadable = null

  for (const p of status) {
    const ts = Number(p.ts)
    const volts = pollVoltage(p)
    if (volts != null) out.battery_mv.push({ ts, value: volts })
    if (p.noise_floor != null && Number.isFinite(Number(p.noise_floor))) {
      out.noise_floor.push({ ts, value: Number(p.noise_floor) })
    }
    if (p.reboot) {
      lastRate = null
      lastUnreadable = null
      continue
    }
    const dRecv = p.delta_packets_recv
    const dErr = p.delta_recv_errors
    if (dRecv == null && dErr == null) continue
    const recv = Number(dRecv ?? 0)
    const err = Number(dErr ?? 0)
    const dur = Number(p.since_prev_secs ?? 0)
    if (recv === 0 && err === 0) {
      if (lastRate != null) out.recv_rate.push({ ts, value: lastRate })
      if (lastUnreadable != null) out.unreadable_pct.push({ ts, value: lastUnreadable })
      continue
    }
    if (dur > 0 && recv >= 0) {
      lastRate = recv / (dur / 3600)
      out.recv_rate.push({ ts, value: lastRate })
    }
    const total = recv + err
    if (total > 0) {
      lastUnreadable = (err / total) * 100
      out.unreadable_pct.push({ ts, value: lastUnreadable })
    }
  }

  for (const p of telemetry) {
    const ts = Number(p.ts)
    if (p.temperature != null && Number.isFinite(Number(p.temperature))) {
      out.temperature.push({ ts, value: Number(p.temperature) })
    }
  }
  for (const p of history?.sun || []) {
    const ts = Number(p.ts)
    const elev = Number(p.value)
    if (Number.isFinite(ts) && Number.isFinite(elev)) {
      out.sun.push({ ts, value: elev })
    }
  }
  return out
}

/** Merge sun elevation series into poll-derived spark data. */
export function withSunSeries(fromPolls, sunRows) {
  const out = { ...fromPolls, sun: [] }
  for (const p of sunRows || []) {
    const ts = Number(p.ts)
    const elev = Number(p.value)
    if (Number.isFinite(ts) && Number.isFinite(elev)) {
      out.sun.push({ ts, value: elev })
    }
  }
  return out
}

/** Evenly spaced sparkline (legacy). */
export function sparklinePath(points, width, height, padding = 2) {
  const values = validPoints(points).map((p) => p.value)
  if (values.length < 2) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const innerW = width - padding * 2
  const innerH = height - padding * 2
  const step = innerW / (values.length - 1)
  const coords = values.map((v, i) => {
    const x = padding + i * step
    const y = padding + innerH - ((v - min) / span) * innerH
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  return coords.join(' ')
}

/** @deprecated use sparklineWallTime */
export function sparklinePathsWallTime(points, width, height, padding = 2) {
  const model = sparklineWallTime(points, width, height, { padding })
  if (!model?.line) return null
  return [model.line]
}

/**
 * Render an inline SVG sparkline into a container element.
 * @param {HTMLElement | null} container
 * @param {Array<{ value?: number }>} points
 * @param {{ width?: number, height?: number, stroke?: string, label?: string, minSpan?: number }} [opts]
 */
export function renderSparkline(container, points, opts = {}) {
  if (!container) return
  const width = opts.width ?? 120
  const height = opts.height ?? 28
  const stroke = opts.stroke ?? '#4ea1ff'
  container.innerHTML = ''
  container.classList.remove('spark-empty')
  const model = sparklineWallTime(points, width, height, { minSpan: opts.minSpan })
  if (!model) {
    container.classList.add('spark-empty')
    container.textContent = '—'
    return
  }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`)
  svg.setAttribute('aria-hidden', 'true')
  if (model.line) {
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline')
    poly.setAttribute('fill', 'none')
    poly.setAttribute('stroke', stroke)
    poly.setAttribute('stroke-width', '1.5')
    poly.setAttribute('points', model.line)
    svg.appendChild(poly)
  }
  for (const d of model.dots) {
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle')
    c.setAttribute('cx', String(d.x))
    c.setAttribute('cy', String(d.y))
    c.setAttribute('r', '1.8')
    c.setAttribute('fill', stroke)
    svg.appendChild(c)
  }
  container.appendChild(svg)
  if (opts.label) container.setAttribute('title', opts.label)
}

/** @param {string} grade */
export function healthStroke(grade) {
  if (grade === 'bad') return '#f06e6e'
  if (grade === 'warn') return '#f0b86e'
  if (grade === 'ok') return '#6ee7a0'
  return '#4ea1ff'
}
