import { reactive } from 'vue'
import { buildNeighborEdges } from './map.js?v=32'

/** @typedef {{ ts: number, [key: string]: unknown }} HistoryRow */
/** @typedef {{ status: HistoryRow[], telemetry: HistoryRow[], polls: HistoryRow[], neighbors: HistoryRow[], acl: HistoryRow[], sun?: HistoryRow[] }} UnitHistory */
/** @typedef {{ key: string, history?: UnitHistory, [key: string]: unknown }} FleetUnit */
/** @typedef {{ units: Record<string, FleetUnit>, counts: Record<string, unknown>, poll: Record<string, unknown>, companion: string | null, edges: unknown[] }} FleetSnapshot */

const HISTORY_LIMIT = 48
const POLL_MERGE_WINDOW = 120

function emptyHistory() {
  return { status: [], telemetry: [], polls: [], neighbors: [], acl: [], sun: [] }
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

/** @param {HistoryRow} row @param {HistoryRow | null | undefined} prev */
function applyPollDeltasFront(row, prev) {
  if (!prev) return
  row.since_prev_secs = Math.max(0, Number(row.ts) - Number(prev.ts))
  const prevUp = prev.uptime_secs
  const currUp = row.uptime_secs
  const reboot =
    prevUp != null && currUp != null && Number(currUp) < Number(prevUp)
  row.reboot = reboot
  if (reboot) return
  for (const key of ['packets_recv', 'packets_sent', 'recv_errors']) {
    const curr = row[key]
    const pval = prev[key]
    if (curr == null || pval == null) continue
    const delta = Number(curr) - Number(pval)
    if (delta >= 0) row[`delta_${key}`] = delta
  }
  for (const [key, digits] of [
    ['voltage', 3],
    ['temperature', 1],
    ['battery_mv', 0],
  ]) {
    const curr = row[key]
    const pval = prev[key]
    if (curr == null || pval == null) continue
    const a = Number(curr)
    const b = Number(pval)
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue
    row[`delta_${key}`] = Number((a - b).toFixed(digits))
  }
}

/** @param {UnitHistory} hist @param {'status' | 'telemetry'} source @param {HistoryRow} row */
function mergePollSample(hist, source, row) {
  if (!Array.isArray(hist.polls)) hist.polls = []
  const polls = hist.polls
  if (source === 'status') {
    if (polls.length && polls[0].ts === row.ts) {
      Object.assign(polls[0], row)
      return
    }
    const next = { ...row }
    applyPollDeltasFront(next, polls[0])
    polls.unshift(next)
    if (polls.length > HISTORY_LIMIT) polls.length = HISTORY_LIMIT
    return
  }
  if (source === 'telemetry') {
    let best = -1
    let bestDt = POLL_MERGE_WINDOW + 1
    for (let i = 0; i < polls.length; i++) {
      const dt = Math.abs(Number(polls[i].ts) - Number(row.ts))
      if (dt < bestDt) {
        bestDt = dt
        best = i
      }
    }
    if (best >= 0 && bestDt <= POLL_MERGE_WINDOW) {
      Object.assign(polls[best], row)
      return
    }
    const next = { ...row }
    applyPollDeltasFront(next, polls[0])
    polls.unshift(next)
    if (polls.length > HISTORY_LIMIT) polls.length = HISTORY_LIMIT
  }
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
  if (source === 'status' || source === 'telemetry') {
    mergePollSample(hist, source, row)
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
  if (
    !('unit' in poll) &&
    (poll.phase === 'idle' || poll.phase === 'done')
  ) {
    delete fleetStore.poll.unit
  }
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
    polls: histories.polls || [],
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
