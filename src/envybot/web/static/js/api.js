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
