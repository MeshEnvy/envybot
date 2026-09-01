/** @param {Array<{ value?: number }>} points */
export function sparklinePath(points, width, height, padding = 2) {
  const values = points
    .map((p) => p.value)
    .filter((v) => v != null && Number.isFinite(Number(v)))
    .map(Number)
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

/**
 * Render an inline SVG sparkline into a container element.
 * @param {HTMLElement | null} container
 * @param {Array<{ value?: number }>} points
 * @param {{ width?: number, height?: number, stroke?: string, label?: string }} [opts]
 */
export function renderSparkline(container, points, opts = {}) {
  if (!container) return
  const width = opts.width ?? 120
  const height = opts.height ?? 28
  const stroke = opts.stroke ?? '#4ea1ff'
  container.innerHTML = ''
  container.classList.remove('spark-empty')
  const path = sparklinePath(points, width, height)
  if (!path) {
    container.classList.add('spark-empty')
    container.textContent = '—'
    return
  }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`)
  svg.setAttribute('aria-hidden', 'true')
  const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline')
  poly.setAttribute('fill', 'none')
  poly.setAttribute('stroke', stroke)
  poly.setAttribute('stroke-width', '1.5')
  poly.setAttribute('points', path)
  svg.appendChild(poly)
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
