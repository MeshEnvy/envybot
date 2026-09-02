import { reactive } from 'vue'
import { buildNeighborEdges } from './map.js?v=27'

/** @typedef {{ ts: number, [key: string]: unknown }} HistoryRow */
/** @typedef {{ status: HistoryRow[], telemetry: HistoryRow[], neighbors: HistoryRow[], acl: HistoryRow[], sun?: HistoryRow[] }} UnitHistory */
/** @typedef {{ key: string, history?: UnitHistory, [key: string]: unknown }} FleetUnit */
/** @typedef {{ units: Record<string, FleetUnit>, counts: Record<string, unknown>, poll: Record<string, unknown>, companion: string | null, edges: unknown[] }} FleetSnapshot */

const HISTORY_LIMIT = 48

function emptyHistory() {
  return { status: [], telemetry: [], neighbors: [], acl: [], sun: [] }
}

export const fleetStore = reactive({
  units: /** @type {Record<string, FleetUnit>} */ ({}),
  counts: /** @type {Record<string, unknown>} */ ({}),
  poll: /** @type {Record<string, unknown>} */ ({}),
  companion: /** @type {string | null} */ (null),
  edges: /** @type {unknown[]} */ ([]),
  now: Math.floor(Date.now() / 1000),
})

/** @type {ReturnType<typeof setInterval> | null} */
let clockId = null

export function startClock() {
  stopClock()
  fleetStore.now = Math.floor(Date.now() / 1000)
  clockId = setInterval(() => {
    fleetStore.now = Math.floor(Date.now() / 1000)
  }, 1000)
}

export function stopClock() {
  if (clockId == null) return
  clearInterval(clockId)
  clockId = null
}

function ensureUnit(key) {
  if (!fleetStore.units[key]) {
    fleetStore.units[key] = { key, history: emptyHistory() }
  } else if (!fleetStore.units[key].history) {
    fleetStore.units[key].history = emptyHistory()
  }
  return fleetStore.units[key]
}

function prependHistory(key, source, row) {
  const unit = ensureUnit(key)
  const hist = unit.history || emptyHistory()
  const list = hist[source]
  if (!Array.isArray(list)) return
  if (list.length && list[0].ts === row.ts) {
    Object.assign(list[0], row)
  } else {
    list.unshift(row)
    if (list.length > HISTORY_LIMIT) list.length = HISTORY_LIMIT
  }
}

function recomputeEdges() {
  fleetStore.edges = buildNeighborEdges(fleetStore.units)
}

/** @param {FleetSnapshot} snap */
export function replaceSnapshot(snap) {
  const incoming = snap.units || {}
  const merged = {}
  for (const [key, unit] of Object.entries(incoming)) {
    const prev = fleetStore.units[key]
    merged[key] = {
      ...(prev || {}),
      ...unit,
      history: prev?.history || emptyHistory(),
    }
  }
  fleetStore.units = merged
  fleetStore.edges = Array.isArray(snap.edges) ? snap.edges : buildNeighborEdges(merged)
  fleetStore.counts = snap.counts || {}
  fleetStore.poll = { ...(snap.poll || {}) }
  if (snap.companion != null) fleetStore.companion = snap.companion
}

/** @param {FleetUnit} unit */
export function patchUnit(unit) {
  const key = String(unit.key)
  const prev = fleetStore.units[key] || { key, history: emptyHistory() }
  fleetStore.units[key] = {
    ...prev,
    ...unit,
    history: prev.history || emptyHistory(),
  }
  const sample = unit.sample
  if (sample && sample.source && sample.row) {
    prependHistory(key, sample.source, sample.row)
    const ts = sample.row.ts
    if (typeof ts === 'number' && Number.isFinite(ts)) {
      const heard = fleetStore.units[key].last_heard
      if (heard == null || ts > Number(heard)) {
        fleetStore.units[key].last_heard = ts
      }
    }
    delete fleetStore.units[key].sample
  }
  recomputeEdges()
}

/** @param {Record<string, unknown>} poll */
export function patchSession(poll) {
  Object.assign(fleetStore.poll, poll)
  if (poll.companion != null) fleetStore.companion = poll.companion
}

/** @param {Record<string, unknown>} event */
export function patchConsole(event) {
  const poll = fleetStore.poll
  if (!poll.console || typeof poll.console !== 'object') {
    poll.console = { tabs: [] }
  }
  const console = /** @type {{ tabs?: Record<string, unknown>[] }} */ (poll.console)
  const tabs = Array.isArray(console.tabs) ? console.tabs : []
  const tabId = event.tab_id
  if (event.state === 'closed' && typeof tabId === 'string') {
    console.tabs = tabs.filter((t) => t.tab_id !== tabId)
    return
  }
  if (typeof tabId !== 'string' || !tabId) return
  const next = tabs.slice()
  const i = next.findIndex((t) => t.tab_id === tabId)
  if (i >= 0) next[i] = event
  else next.push(event)
  console.tabs = next
}

/** @param {string} key @param {UnitHistory} histories */
export function loadHistories(key, histories) {
  const unit = ensureUnit(key)
  unit.history = {
    status: histories.status || [],
    telemetry: histories.telemetry || [],
    neighbors: histories.neighbors || [],
    acl: histories.acl || [],
    sun: histories.sun || [],
  }
}

/** @param {string} key */
export function clearHistories(key) {
  if (fleetStore.units[key]) {
    fleetStore.units[key].history = emptyHistory()
  }
}
