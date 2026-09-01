/** @typedef {import('./state.js').FleetSnapshot} FleetSnapshot */

/** @returns {Promise<FleetSnapshot>} */
export async function fetchFleet() {
  const res = await fetch('/api/fleet')
  if (!res.ok) throw new Error(`fleet fetch ${res.status}`)
  return /** @type {FleetSnapshot} */ (await res.json())
}

/** @param {{ onHello: (s: FleetSnapshot) => void, onUnit: (u: Record<string, unknown>) => void, onSession: (p: Record<string, unknown>) => void }} handlers */
export function connectEvents(handlers) {
  const es = new EventSource('/events')
  es.addEventListener('hello', (ev) => {
    handlers.onHello(JSON.parse(/** @type {MessageEvent} */ (ev).data))
  })
  es.addEventListener('unit', (ev) => {
    handlers.onUnit(JSON.parse(/** @type {MessageEvent} */ (ev).data))
  })
  es.addEventListener('session', (ev) => {
    handlers.onSession(JSON.parse(/** @type {MessageEvent} */ (ev).data))
  })
  es.onerror = () => {
    /* browser reconnects automatically */
  }
  return es
}

/** @param {string} key @param {Record<string, unknown>} body */
export async function patchUnit(key, body) {
  const res = await fetch(`/api/unit/${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`unit edit ${res.status}`)
  return res.json()
}

/** @param {string} key */
export async function queueUnit(key) {
  const res = await fetch(`/api/queue/${encodeURIComponent(key)}`, { method: 'POST' })
  if (!res.ok) throw new Error(`queue ${res.status}`)
  return res.json()
}

/** @param {string} unit @param {string} metric */
export async function fetchHistory(unit, metric) {
  const res = await fetch(`/api/history/${encodeURIComponent(unit)}?metric=${encodeURIComponent(metric)}`)
  if (!res.ok) throw new Error(`history ${res.status}`)
  return res.json()
}
