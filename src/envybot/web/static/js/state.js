import { reactive } from 'vue'
import { buildNeighborEdges } from './map.js?v=22'

/** @typedef {{ ts: number, [key: string]: unknown }} HistoryRow */
/** @typedef {{ status: HistoryRow[], telemetry: HistoryRow[], neighbors: HistoryRow[], acl: HistoryRow[] }} UnitHistory */
/** @typedef {{ key: string, history?: UnitHistory, [key: string]: unknown }} FleetUnit */
/** @typedef {{ units: Record<string, FleetUnit>, counts: Record<string, unknown>, poll: Record<string, unknown>, companion: string | null, edges: unknown[] }} FleetSnapshot */

const HISTORY_LIMIT = 48

function emptyHistory() {
  return { status: [], telemetry: [], neighbors: [], acl: [] }
}

export const fleetStore = reactive({
  units: /** @type {Record<string, FleetUnit>} */ ({}),
  counts: /** @type {Record<string, unknown>} */ ({}),
  poll: /** @type {Record<string, unknown>} */ ({}),
  companion: /** @type {string | null} */ (null),
  edges: /** @type {unknown[]} */ ([]),
})

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
    delete fleetStore.units[key].sample
  }
  recomputeEdges()
}

/** @param {Record<string, unknown>} poll */
export function patchSession(poll) {
  Object.assign(fleetStore.poll, poll)
  if (poll.companion != null) fleetStore.companion = poll.companion
}

/** @param {string} key @param {UnitHistory} histories */
export function loadHistories(key, histories) {
  const unit = ensureUnit(key)
  unit.history = {
    status: histories.status || [],
    telemetry: histories.telemetry || [],
    neighbors: histories.neighbors || [],
    acl: histories.acl || [],
  }
}

/** @param {string} key */
export function clearHistories(key) {
  if (fleetStore.units[key]) {
    fleetStore.units[key].history = emptyHistory()
  }
}
