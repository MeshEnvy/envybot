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

/** @param {string} key @param {'refresh' | 'pull' | 'push'} job */
async function postManualJob(key, job) {
  const res = await fetch(`/api/${job}/${encodeURIComponent(key)}`, { method: 'POST' })
  if (!res.ok) throw new Error(`${job} ${res.status}`)
  return res.json()
}

/** @param {string} key */
export function refreshUnit(key) {
  return postManualJob(key, 'refresh')
}

/** @param {string} key */
export function pullUnit(key) {
  return postManualJob(key, 'pull')
}

/** @param {string} key */
export function pushUnit(key) {
  return postManualJob(key, 'push')
}

/** @param {string} unit @param {string} metric @param {number} [hours] */
export async function fetchHistory(unit, metric, hours = 72) {
  const q = new URLSearchParams({ metric, hours: String(hours) })
  const res = await fetch(`/api/history/${encodeURIComponent(unit)}?${q}`)
  if (!res.ok) throw new Error(`history ${res.status}`)
  return res.json()
}

/** @param {string} unit @param {number} [hours] @param {number} [limit] */
export async function fetchPolls(unit, hours = 72, limit = 48) {
  const q = new URLSearchParams({ hours: String(hours), limit: String(limit) })
  const res = await fetch(`/api/polls/${encodeURIComponent(unit)}?${q}`)
  if (!res.ok) throw new Error(`polls ${res.status}`)
  return res.json()
}
