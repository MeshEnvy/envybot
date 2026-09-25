/** @typedef {import('./state.js').FleetSnapshot} FleetSnapshot */

/** @returns {Promise<FleetSnapshot>} */
export async function fetchFleet() {
  const res = await fetch('/api/fleet')
  if (!res.ok) throw new Error(`fleet fetch ${res.status}`)
  return /** @type {FleetSnapshot} */ (await res.json())
}

/** @param {{ onHello: (s: FleetSnapshot) => void, onUnit: (u: Record<string, unknown>) => void, onSession: (p: Record<string, unknown>) => void, onAudit?: (a: Record<string, unknown>) => void, onConsole?: (c: Record<string, unknown>) => void }} handlers */
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
  if (handlers.onAudit) {
    es.addEventListener('audit', (ev) => {
      handlers.onAudit(JSON.parse(/** @type {MessageEvent} */ (ev).data))
    })
  }
  if (handlers.onConsole) {
    es.addEventListener('console', (ev) => {
      handlers.onConsole(JSON.parse(/** @type {MessageEvent} */ (ev).data))
    })
  }
  es.onerror = () => {
    /* browser reconnects automatically */
  }
  return es
}

/** @param {number} lat @param {number} lon */
export async function postBenchLoc(lat, lon) {
  const res = await fetch('/api/bench', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `bench ${res.status}`)
  }
  return res.json()
}

/** @param {string} key @param {string} paste */
export async function postUnitPath(key, paste) {
  const res = await fetch(`/api/unit/${encodeURIComponent(key)}/path`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paste }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error || `path pin ${res.status}`)
  return body
}

/** @param {string} key */
export async function clearUnitPath(key) {
  const res = await fetch(`/api/unit/${encodeURIComponent(key)}/path`, {
    method: 'DELETE',
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error || `path clear ${res.status}`)
  return body
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

/** @param {string} key @param {'refresh' | 'pull' | 'deploy'} job */
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
export function deployUnit(key) {
  return postManualJob(key, 'deploy')
}

/** @param {string} key @param {string | number} selector catalog index or hex mid */
export async function stageUnit(key, selector) {
  const res = await fetch(`/api/stage/${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: selector }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `stage ${res.status}`)
  }
  return res.json()
}

/** @param {string} key */
export async function installUnit(key) {
  const res = await fetch(`/api/install/${encodeURIComponent(key)}`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `install ${res.status}`)
  }
  return res.json()
}

/** @param {string} key @param {string} [tabId] */
export async function openConsole(key, tabId) {
  const res = await fetch('/api/console/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(tabId ? { key, tab_id: tabId } : { key }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console open ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId @param {string} cmd */
export async function sendConsole(tabId, cmd) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console send ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId @param {string} cmd @param {string | null} [error] */
export async function parkConsole(tabId, cmd, error) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/park`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cmd, error: error || 'command timeout' }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console park ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId */
export async function retryConsole(tabId) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/retry`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console retry ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId */
export async function skipConsole(tabId) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/skip`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console skip ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId */
export async function cancelConsole(tabId) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/cancel`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console cancel ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId */
export async function closeConsole(tabId) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/close`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console close ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId @param {string} pendingId @param {string} cmd */
export async function patchConsolePending(tabId, pendingId, cmd) {
  const res = await fetch(
    `/api/console/${encodeURIComponent(tabId)}/pending/${encodeURIComponent(pendingId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd }),
    }
  )
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console pending ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId @param {string} [pendingId] */
export async function deleteConsolePending(tabId, pendingId) {
  const suffix = pendingId
    ? `/pending/${encodeURIComponent(pendingId)}`
    : '/pending'
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}${suffix}`, {
    method: 'DELETE',
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console pending ${res.status}`)
  }
  return res.json()
}

/** @param {string} tabId */
export async function clearConsoleHistory(tabId) {
  const res = await fetch(`/api/console/${encodeURIComponent(tabId)}/clear`, { method: 'POST' })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `console clear ${res.status}`)
  }
  return res.json()
}

/** @param {string} unit @param {string} metric @param {number} [hours] */
export async function fetchHistory(unit, metric, hours = 72) {
  const q = new URLSearchParams({ metric, hours: String(hours) })
  const res = await fetch(`/api/history/${encodeURIComponent(unit)}?${q}`)
  if (!res.ok) throw new Error(`history ${res.status}`)
  return res.json()
}

/** @typedef {{ status: unknown[], telemetry: unknown[], neighbors: unknown[], acl: unknown[], sun?: unknown[] }} SourceHistories */

/** @param {string} unit @param {number} [hours] @param {number} [limit] @returns {Promise<{ unit: string, hours: number, histories: SourceHistories }>} */
export async function fetchPolls(unit, hours = 72, limit = 80) {
  const q = new URLSearchParams({ hours: String(hours), limit: String(limit) })
  const res = await fetch(`/api/polls/${encodeURIComponent(unit)}?${q}`)
  if (!res.ok) throw new Error(`polls ${res.status}`)
  return res.json()
}

/** @typedef {{ id: number, ts: number, kind: string, label?: string | null, attempt?: number | null, path: string, wait_s?: number | null, ok: boolean, outcome: string, reply?: string | null, error?: string | null, source?: string | null, duration_s?: number | null }} AuditRow */

/** @param {string} unit @param {{ limit?: number, beforeId?: number }} [opts] @returns {Promise<{ unit: string, rows: AuditRow[], has_more: boolean }>} */
export async function fetchAudit(unit, opts = {}) {
  const q = new URLSearchParams({ limit: String(opts.limit ?? 40) })
  if (opts.beforeId != null) q.set('before_id', String(opts.beforeId))
  const res = await fetch(`/api/audit/${encodeURIComponent(unit)}?${q}`)
  if (!res.ok) throw new Error(`audit ${res.status}`)
  return res.json()
}
