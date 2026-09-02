import { createApp, computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { connectEvents, fetchFleet, fetchPolls, installUnit, patchUnit, pullUnit, pushUnit, refreshUnit, stageUnit, openConsole as apiOpenConsole, sendConsole as apiSendConsole, cancelConsole as apiCancelConsole, closeConsole as apiCloseConsole } from './api.js'
import {
  clearHistories,
  fleetStore,
  loadHistories,
  patchConsole,
  patchSession,
  patchUnit as storePatchUnit,
  replaceSnapshot,
  startClock,
  stopClock,
} from './state.js?v=4'
import {
  formatAgo,
  formatBattery,
  formatCount,
  formatPollWindow,
  formatSignedDelta,
  formatUnreadableRf,
  formatRelative,
  formatRssi,
  formatSite,
  formatSnr,
  formatTemp,
  formatUptime,
  sunEmoji,
  sunElev,
  sunTitle,
  hasHealthIssues,
  hasTrafficStats,
  healthHeadline,
  healthMark,
  healthTooltip,
  compareUnits,
  unitLabel,
  unitTitle,
} from './format.js?v=23'
import { buildNeighborEdges, createMapController, unitStage, unitStatus } from './map.js?v=27'
import { seriesFromHistories, sparklineWallTime, SPARK_MIN_SPAN } from './sparklines.js?v=9'

const App = {
  setup() {
    const fleet = fleetStore
    const selectedKey = ref(null)
    const search = ref('')
    const listFilter = ref('all')
    const aliasDraft = ref('')
    const notesDraft = ref('')
    const aliasEditing = ref(false)
    const notesEditing = ref(false)
    const historyHours = 72
    const LOG_PAGE = 10
    const logShown = reactive({
      status: LOG_PAGE,
      telemetry: LOG_PAGE,
      neighbors: LOG_PAGE,
      acl: LOG_PAGE,
    })
    const consoleMode = ref(false)
    /** @type {Record<string, { cmd: string, reply?: string, error?: string, pending?: boolean }[]>} */
    const consoleHistoryByKey = reactive({})
    /** @type {Record<string, string>} */
    const consoleDraftByKey = reactive({})
    const consoleTranscript = computed(() => transcriptFor(selectedKey.value))
    const consoleDraft = computed({
      get() {
        const key = selectedKey.value
        if (!key) return ''
        return consoleDraftByKey[key] ?? ''
      },
      set(value) {
        const key = selectedKey.value
        if (key) consoleDraftByKey[key] = value
      },
    })
    /** @type {import('vue').Ref<Record<string, unknown> | null>} */
    const consoleLive = ref(null)
    /** @type {import('vue').Ref<HTMLElement | null>} */
    const consoleScroll = ref(null)

    function resetLogShown() {
      logShown.status = LOG_PAGE
      logShown.telemetry = LOG_PAGE
      logShown.neighbors = LOG_PAGE
      logShown.acl = LOG_PAGE
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function logSlice(source) {
      const list = unitHistory.value?.[source]
      if (!Array.isArray(list)) return []
      return list.slice(0, logShown[source])
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function logHasMore(source) {
      const list = unitHistory.value?.[source]
      return Array.isArray(list) && list.length > logShown[source]
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function loadMoreLog(source) {
      logShown[source] += LOG_PAGE
    }

    const METRIC_ROWS = [
      { key: 'sun', label: 'Sun', stroke: '#f5c14a', wave: true },
      { key: 'battery_mv', label: 'Voltage', stroke: '#6ee7a0' },
      { key: 'temperature', label: 'Temp', stroke: '#f0b86e' },
      { key: 'unreadable_pct', label: 'Unreadable', stroke: '#f06e6e' },
      { key: 'recv_rate', label: 'In / h', stroke: '#4ea1ff' },
      { key: 'noise_floor', label: 'Noise', stroke: '#a78bfa' },
    ]
    /** @type {ReturnType<typeof createMapController> | null} */
    let mapCtrl = null
    /** @type {EventSource | null} */
    let es = null

    function applyHello(snap) {
      replaceSnapshot(snap)
    }

    function applyUnit(unit) {
      storePatchUnit(unit)
    }

    function applySession(poll) {
      patchSession(poll)
    }

    const manualAccepting = computed(() => !!fleet.poll?.accepting)

    const openConsoleKey = computed(() => {
      const c = fleet.poll?.console
      if (!c || typeof c !== 'object') return null
      const key = c.key
      const state = c.state
      if (typeof key !== 'string' || state === 'closed') return null
      return key
    })

    const consoleBusy = computed(() => {
      const ev = consoleLive.value
      return ev?.state === 'sending' || ev?.state === 'acquiring'
    })

    const consoleStatusLine = computed(() => {
      const ev = consoleLive.value
      if (!ev) return ''
      if (ev.state === 'acquiring') return 'Logging in…'
      if (ev.state === 'sending' && ev.attempt && ev.max_attempts) {
        return `(attempt ${ev.attempt}/${ev.max_attempts}…)`
      }
      return ''
    })

    const MANUAL_BUSY = new Set([
      'queued',
      'refreshing',
      'pulling',
      'pushing',
      'staging',
      'installing',
      'polling',
    ])

    /** @param {Record<string, unknown> | undefined} unit */
    function isInFlight(unit) {
      const s = unit?.session
      const state = s && typeof s === 'object' && 'state' in s ? s.state : null
      return MANUAL_BUSY.has(state)
    }

    /** @param {Record<string, unknown> | undefined} unit @param {'refresh' | 'pull' | 'push'} [job] */
    function canManualUnit(unit, job) {
      if (!unit || !manualAccepting.value) return false
      return true
    }

    function consoleFromLocation() {
      return new URLSearchParams(location.search).get('console') === '1'
    }

    function activeConsoleKey() {
      const held = openConsoleKey.value
      if (held) return held
      if (consoleMode.value && selectedKey.value) return selectedKey.value
      return null
    }

    /** @param {string | null | undefined} key */
    function transcriptFor(key) {
      const k = key ? String(key).toLowerCase() : ''
      if (!k) return []
      if (!consoleHistoryByKey[k]) consoleHistoryByKey[k] = []
      return consoleHistoryByKey[k]
    }

    function clearConsoleHistory() {
      const key = selectedKey.value
      if (!key) return
      const k = String(key).toLowerCase()
      consoleHistoryByKey[k] = []
      delete consoleDraftByKey[k]
    }

    function scrollConsoleBottom() {
      requestAnimationFrame(() => {
        const el = consoleScroll.value
        if (el) el.scrollTop = el.scrollHeight
      })
    }

    /** @param {Record<string, unknown>} event */
    function applyConsoleEvent(event) {
      patchConsole(event)
      consoleLive.value = event
      if (event.state === 'ready' && event.reply != null) {
        const rows = consoleTranscript.value
        for (let i = rows.length - 1; i >= 0; i -= 1) {
          if (rows[i].pending) {
            rows[i].pending = false
            rows[i].reply = String(event.reply)
            break
          }
        }
      }
      if (event.state === 'ready' && event.error) {
        const rows = consoleTranscript.value
        for (let i = rows.length - 1; i >= 0; i -= 1) {
          if (rows[i].pending) {
            rows[i].pending = false
            rows[i].error = String(event.error)
            break
          }
        }
      }
      scrollConsoleBottom()
    }

    /** @param {Record<string, unknown>} unit */
    async function startConsole(unit) {
      if (!unit?.key) return
      consoleMode.value = true
      consoleLive.value = { state: 'ready', key: unit.key }
      syncLocation(String(unit.key), { console: true })
      try {
        const ev = await apiOpenConsole(String(unit.key))
        consoleLive.value = ev
        patchConsole(ev)
      } catch (err) {
        console.error(err)
        consoleLive.value = { state: 'ready', key: unit.key, error: String(err) }
      }
    }

    async function exitConsole() {
      const key = activeConsoleKey()
      if (key) {
        try {
          await apiCloseConsole(key)
        } catch (err) {
          console.error(err)
        }
      }
      patchConsole({ state: 'closed' })
      consoleMode.value = false
      consoleLive.value = null
      syncLocation(selectedKey.value, { console: false })
    }

    async function submitConsoleLine() {
      const key = selectedKey.value
      const cmd = consoleDraft.value.trim()
      if (!key || !cmd || consoleBusy.value) return
      consoleTranscript.value.push({ cmd, pending: true })
      consoleDraft.value = ''
      scrollConsoleBottom()
      try {
        const res = await apiSendConsole(key, cmd)
        const rows = consoleTranscript.value
        const row = rows[rows.length - 1]
        if (row && row.cmd === cmd) {
          row.pending = false
          if (res.error) row.error = String(res.error)
          else row.reply = res.reply != null ? String(res.reply) : ''
        }
      } catch (err) {
        const rows = consoleTranscript.value
        const row = rows[rows.length - 1]
        if (row) {
          row.pending = false
          row.error = String(err)
        }
      }
      scrollConsoleBottom()
    }

    async function cancelConsoleSend() {
      const key = selectedKey.value
      if (!key) return
      try {
        await apiCancelConsole(key)
      } catch (err) {
        console.error(err)
      }
    }

    async function maybeReopenConsole() {
      const key = unitKeyFromLocation()
      if (!key || !consoleFromLocation()) return
      if (openConsoleKey.value === key) {
        consoleMode.value = true
        return
      }
      const unit = fleet.units[key]
      if (unit) await startConsole(unit)
    }

    /** @param {Record<string, unknown> | null | undefined} ota */
    function otaLocalLabel(ota) {
      if (!ota || typeof ota !== 'object') return '—'
      const local =
        /** @type {{ state?: string, mid?: string, pct?: number, have?: number, total?: number, age_s?: number, status_word?: string }} */ (
          ota
        ).local
      if (!local || !local.state || local.state === 'none') return 'no download'
      const bits = []
      if (local.status_word) bits.push(local.status_word)
      else bits.push(local.state)
      if (local.have != null && local.total != null) bits.push(`${local.have}/${local.total}`)
      if (local.pct != null) bits.push(`${local.pct}%`)
      if (local.mid) bits.push(`id=${local.mid}`)
      if (local.age_s != null) bits.push(`${local.age_s}s`)
      return bits.join(' ')
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaHwLabel(running) {
      if (!running || typeof running !== 'object') return '—'
      const hw = /** @type {{ hw_id?: string | null }} */ (running).hw_id
      if (hw == null || hw === '') return '—'
      return hw
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaTargetLabel(running) {
      if (!running || typeof running !== 'object') return '—'
      const r = /** @type {{ target_env?: string, target_id?: string }} */ (running)
      const env = r.target_env && r.target_env !== '?' ? r.target_env : ''
      if (env && r.target_id) return `${env} (${r.target_id})`
      return env || r.target_id || '—'
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function otaBodyLabel(unit) {
      if (!unit) return '—'
      const running =
        unit.ota && typeof unit.ota === 'object'
          ? /** @type {{ running?: { body_hash?: string, image_kib?: number } }} */ (unit.ota).running
          : null
      const full = typeof unit.base_hash === 'string' ? unit.base_hash : ''
      const prefix = running?.body_hash || ''
      const hash = full || prefix || ''
      if (!hash) return '—'
      const kib = running?.image_kib
      return kib != null ? `${hash} (${kib}K)` : hash
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaServingLabel(running) {
      if (!running || typeof running !== 'object' || !('serving' in running)) return '—'
      const r = /** @type {{ serving?: boolean, serving_count?: number }} */ (running)
      const on = r.serving ? 'on' : 'off'
      return r.serving_count != null ? `${on} (${r.serving_count})` : on
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaBlLabel(running) {
      if (!running || typeof running !== 'object' || !('bl_apply' in running)) return '—'
      const r = /** @type {{ bl_apply?: boolean, bl_rc?: string }} */ (running)
      const apply = r.bl_apply ? 'apply' : 'NONE'
      return r.bl_rc ? `${apply} rc=${r.bl_rc}` : apply
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function canInstallUnit(unit) {
      if (!unit || isInFlight(unit) || !manualAccepting.value) return false
      const ota = unit.ota
      if (!ota || typeof ota !== 'object') return false
      const local = /** @type {{ state?: string }} */ (ota).local
      const running = /** @type {{ bl_apply?: boolean }} */ (ota).running
      if (local?.state !== 'ready') return false
      if (running && running.bl_apply === false) return false
      return true
    }

    /** @param {Record<string, unknown> | undefined} unit @param {Record<string, unknown>} row */
    function canStageRow(unit, row) {
      if (!unit || isInFlight(unit) || !manualAccepting.value) return false
      const ota = unit.ota
      const local = ota && typeof ota === 'object' ? /** @type {{ state?: string }} */ (ota).local : null
      if (local?.state === 'downloading' || local?.state === 'ready') return false
      return typeof row.index === 'number' || typeof row.index === 'string'
    }

    /** @param {Record<string, unknown>} unit @param {'refresh' | 'pull' | 'push' | 'stage' | 'install'} job */
    function markOptimistic(unit, job) {
      const key = String(unit.key)
      const prev = fleet.units[key] || unit
      const stateMap = {
        refresh: 'refreshing',
        pull: 'pulling',
        push: 'pushing',
        stage: 'staging',
        install: 'installing',
      }
      const state = stateMap[job] || 'polling'
      fleet.units[key] = {
        ...prev,
        session: {
          ...(typeof prev.session === 'object' && prev.session ? prev.session : {}),
          state,
          stage: 'Logging in',
          kind: 'login',
          manual: true,
          job,
        },
      }
    }

    /** @param {Record<string, unknown>} unit @param {'refresh' | 'pull' | 'push'} job @param {Event} [ev] */
    async function runManualJob(unit, job, ev) {
      ev?.stopPropagation?.()
      if (!canManualUnit(unit, job)) return
      markOptimistic(unit, job)
      pushMap()
      const fn = job === 'refresh' ? refreshUnit : job === 'pull' ? pullUnit : pushUnit
      try {
        const updated = await fn(String(unit.key))
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
      }
    }

    /** @param {Record<string, unknown>} unit @param {Record<string, unknown>} row @param {Event} [ev] */
    async function runStage(unit, row, ev) {
      ev?.stopPropagation?.()
      if (!canStageRow(unit, row)) return
      markOptimistic(unit, 'stage')
      pushMap()
      try {
        const updated = await stageUnit(String(unit.key), row.index)
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
      }
    }

    /** @param {Record<string, unknown>} unit @param {Event} [ev] */
    async function runInstall(unit, ev) {
      ev?.stopPropagation?.()
      if (!canInstallUnit(unit)) return
      const local = unit.ota && typeof unit.ota === 'object' ? /** @type {{ mid?: string }} */ (unit.ota).local : null
      const mid = local?.mid || 'staged update'
      if (
        !window.confirm(
          `Install OTA on ${unit.label || unit.key}? Node will reboot (${mid}). Wrong image can brick until USB recovery.`
        )
      ) {
        return
      }
      markOptimistic(unit, 'install')
      pushMap()
      try {
        const updated = await installUnit(String(unit.key))
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
      }
    }

    const activeCount = computed(() => Object.values(fleet.units || {}).filter(isInFlight).length)

    const sortedUnits = computed(() => {
      const q = search.value.trim().toLowerCase()
      let units = Object.values(fleet.units || {})
      if (listFilter.value === 'active') {
        units = units.filter(isInFlight)
      }
      if (q) {
        units = units.filter((u) => {
          const hay = [u.key, u.unit_id, u.site, u.site_name, u.alias, u.label, u.notes]
            .filter(Boolean)
            .join(' ')
            .toLowerCase()
          return hay.includes(q)
        })
      }
      return units.sort(compareUnits)
    })

    const selectedUnit = computed(() => (selectedKey.value ? fleet.units[selectedKey.value] : null))

    const unitHistory = computed(() => selectedUnit.value?.history || null)

    async function loadHistoriesFor(key) {
      try {
        const res = await fetchPolls(key, historyHours)
        loadHistories(key, res.histories || {})
      } catch {
        clearHistories(key)
      }
    }

    const sparkSeries = computed(() => seriesFromHistories(unitHistory.value || {}))

    const sparkDomain = computed(() => {
      const now = Math.floor(Date.now() / 1000)
      return { tMin: now - historyHours * 3600, tMax: now }
    })

    const sparkModels = computed(() => {
      /** @type {Record<string, ReturnType<typeof sparklineWallTime>>} */
      const out = {}
      const domain = sparkDomain.value
      for (const row of METRIC_ROWS) {
        const wave = !!row.wave
        out[row.key] = sparklineWallTime(sparkSeries.value[row.key] || [], 168, 22, {
          minSpan: SPARK_MIN_SPAN[row.key] || 0,
          tMin: domain.tMin,
          tMax: domain.tMax,
          ...(wave ? { yMin: -90, yMax: 90, dots: false } : {}),
        })
      }
      return out
    })

    const SPARK_VALUE_FORMAT = {
      battery_mv: (v) => `${v.toFixed(2)} V`,
      temperature: (v) => formatTemp(v),
      unreadable_pct: (v) => `${v.toFixed(1)}%`,
      recv_rate: (v) => `${v.toFixed(1)}/h`,
      noise_floor: (v) => `${Math.trunc(v)} dBm`,
    }

    function metricNow(metric) {
      const unit = selectedUnit.value
      if (metric === 'sun') {
        const points = sparkSeries.value.sun || []
        const last = points[points.length - 1]
        if (!last || last.value == null) return '—'
        const elev = Number(last.value)
        return `${elev >= 0 ? '☀️' : '🌙'} ${elev.toFixed(0)}∠`
      }
      if (metric === 'battery_mv') {
        const mv = unit?.status?.battery_mv
        if (mv != null) return formatBattery(mv)
      }
      if (metric === 'temperature') {
        const t = unit?.telemetry?.temperature
        if (t != null) return formatTemp(t)
      }
      const points = sparkSeries.value[metric] || []
      const last = [...points].reverse().find((p) => p.value != null && Number.isFinite(Number(p.value)))
      if (!last) return '—'
      const fmt = SPARK_VALUE_FORMAT[metric] || ((v) => String(v))
      return fmt(Number(last.value))
    }

    /** Latest value as text when there are too few points for a line. */
    function sparkFallback(metric) {
      return metricNow(metric)
    }

    function formatPollDelta(recv, sent) {
      const parts = []
      if (recv != null) parts.push(`+${recv}`)
      if (sent != null) parts.push(`+${sent}`)
      return parts.length ? parts.join(' / ') : '—'
    }

    function formatPollVoltage(poll) {
      if (poll?.battery_mv != null) return formatBattery(poll.battery_mv)
      return '—'
    }

    function formatPollTemp(poll) {
      return poll?.temperature != null ? formatTemp(poll.temperature) : '—'
    }

    function voltageStock(poll) {
      return formatSignedDelta(poll?.delta_voltage, 2)
    }

    function tempStock(poll) {
      const d = poll?.delta_temperature
      if (d == null || !Number.isFinite(Number(d))) return null
      return formatSignedDelta((Number(d) * 9) / 5, 0)
    }

    function unitKeyFromLocation() {
      const raw = new URLSearchParams(location.search).get('unit')
      if (!raw) return null
      const needle = raw.trim()
      if (!needle) return null
      if (fleet.units[needle]) return needle
      const lower = needle.toLowerCase()
      if (fleet.units[lower]) return lower
      for (const [k, u] of Object.entries(fleet.units || {})) {
        if (k.toLowerCase() === lower) return k
        if (String(u.unit_id || '').toLowerCase() === lower) return k
      }
      return lower
    }

    function syncLocation(key, { console: inConsole = false } = {}) {
      const url = new URL(location.href)
      if (key) url.searchParams.set('unit', key)
      else url.searchParams.delete('unit')
      if (inConsole) url.searchParams.set('console', '1')
      else url.searchParams.delete('console')
      const next = `${url.pathname}${url.search}${url.hash}`
      const cur = `${location.pathname}${location.search}${location.hash}`
      if (next === cur) return
      history.replaceState(null, '', next)
    }

    function openUnit(key, { fly = true } = {}) {
      const keepConsole = consoleMode.value && selectedKey.value === key
      selectedKey.value = key
      syncLocation(key, { console: keepConsole })
      if (fly) mapCtrl?.flyTo(key, fleet)
      mapCtrl?.sync(fleet, key, fleet.now)
      loadHistoriesFor(key)
    }

    async function applyLocationUnit() {
      const key = unitKeyFromLocation()
      if (!key) {
        if (selectedKey.value) {
          if (activeConsoleKey()) await exitConsole()
          selectedKey.value = null
          mapCtrl?.sync(fleet, null, fleet.now)
        }
        return
      }
      if (!fleet.units[key]) return
      if (selectedKey.value === key) return
      if (activeConsoleKey()) await exitConsole()
      openUnit(key)
    }

    async function selectUnit(key) {
      if (selectedKey.value === key) {
        await clearSelection()
        return
      }
      if (activeConsoleKey()) await exitConsole()
      openUnit(key)
    }

    async function clearSelection() {
      if (activeConsoleKey()) await exitConsole()
      selectedKey.value = null
      syncLocation(null)
      mapCtrl?.sync(fleet, null, fleet.now)
    }

    const modalDownOnBackdrop = ref(false)

    function onModalBackdropMouseDown() {
      modalDownOnBackdrop.value = true
    }

    function onModalBackdropMouseUp() {
      if (modalDownOnBackdrop.value) clearSelection()
      modalDownOnBackdrop.value = false
    }

    function onModalPanelMouseUp() {
      modalDownOnBackdrop.value = false
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function sessionBadgeTitle(unit) {
      const s = unit?.session
      if (!s || typeof s !== 'object') return ''
      const parts = []
      const attempt = s.attempt
      const max = s.max_attempts
      if (typeof attempt === 'number' && attempt > 0 && typeof max === 'number' && max > 0) {
        parts.push(`${attempt}/${max}`)
      } else if (typeof attempt === 'number' && attempt > 0) {
        parts.push(`attempt ${attempt}`)
      }
      if (typeof s.error === 'string' && s.error) parts.push(s.error)
      return parts.join(' · ')
    }

    function cardPrimary(unit) {
      return unitLabel(unit)
    }

    function cardNodeId(unit) {
      return String(unit.unit_id || unit.key || '')
    }

    function cardShowNodeId(unit) {
      const id = cardNodeId(unit)
      return !!id && id !== cardPrimary(unit)
    }

    function syncBookDrafts(unit) {
      if (!unit || aliasEditing.value || notesEditing.value) return
      aliasDraft.value = typeof unit.alias === 'string' ? unit.alias : ''
      notesDraft.value = typeof unit.notes === 'string' ? unit.notes : ''
    }

    watch(selectedKey, async (key, prev) => {
      if (prev && key !== prev && activeConsoleKey()) {
        await exitConsole()
      }
      aliasEditing.value = false
      notesEditing.value = false
      resetLogShown()
      syncBookDrafts(selectedUnit.value)
      syncLocation(key)
    })

    watch(
      () => selectedUnit.value?.alias,
      () => syncBookDrafts(selectedUnit.value)
    )
    watch(
      () => selectedUnit.value?.notes,
      () => syncBookDrafts(selectedUnit.value)
    )

    /** @param {Record<string, unknown>} unit */
    async function saveAlias(unit) {
      aliasEditing.value = false
      const next = aliasDraft.value.trim()
      const prev = typeof unit.alias === 'string' ? unit.alias : ''
      if (next === prev) return
      try {
        const updated = await patchUnit(String(unit.key), { alias: next || null })
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
        aliasDraft.value = prev
      }
    }

    /** @param {Record<string, unknown>} unit */
    async function saveNotes(unit) {
      notesEditing.value = false
      const next = notesDraft.value
      const prev = typeof unit.notes === 'string' ? unit.notes : ''
      if (next === prev) return
      try {
        const updated = await patchUnit(String(unit.key), { notes: next.trim() ? next : null })
        applyUnit(updated)
      } catch (err) {
        console.error(err)
        notesDraft.value = prev
      }
    }

    async function togglePublic(unit, ev) {
      try {
        const updated = await patchUnit(unit.key, { public: ev.target.checked })
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
      }
    }

    /** @param {Record<string, unknown>} unit @param {Event} [ev] */
    async function togglePaused(unit, ev) {
      ev?.stopPropagation?.()
      const next = ev && ev.target && 'checked' in ev.target ? !!ev.target.checked : !unit.paused
      try {
        const updated = await patchUnit(String(unit.key), { paused: next })
        applyUnit(updated)
        pushMap()
      } catch (err) {
        console.error(err)
      }
    }
    function pushMap() {
      mapCtrl?.sync(fleet, selectedKey.value, fleet.now)
    }

    onMounted(async () => {
      // Let Vue finish layout so #map has non-zero size before MapLibre inits.
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
      startClock()
      mapCtrl = createMapController('map', selectUnit, clearSelection)
      watch(
        () => fleet.now,
        () => pushMap()
      )
      const onKey = (e) => {
        if (e.key === 'Escape') clearSelection()
      }
      const onPop = () => applyLocationUnit()
      window.addEventListener('keydown', onKey)
      window.addEventListener('popstate', onPop)
      onUnmounted(() => {
        window.removeEventListener('keydown', onKey)
        window.removeEventListener('popstate', onPop)
      })
      try {
        applyHello(await fetchFleet())
        applyLocationUnit()
        await maybeReopenConsole()
      } catch (err) {
        console.error(err)
      }
      pushMap()
      // Second sync after layout/tiles settle.
      requestAnimationFrame(() => pushMap())
      es = connectEvents({
        onHello: (snap) => {
          applyHello(snap)
          applyLocationUnit()
          maybeReopenConsole()
          pushMap()
        },
        onUnit: (unit) => {
          applyUnit(unit)
          pushMap()
        },
        onSession: (poll) => {
          applySession(poll)
        },
        onConsole: (event) => {
          applyConsoleEvent(event)
        },
      })
    })

    watch(search, () => {})

    onUnmounted(() => {
      stopClock()
      es?.close()
      mapCtrl?.destroy()
    })

    return {
      fleet,
      search,
      listFilter,
      activeCount,
      sortedUnits,
      selectedUnit,
      selectedKey,
      manualAccepting,
      canManualUnit,
      runManualJob,
      selectUnit,
      clearSelection,
      onModalBackdropMouseDown,
      onModalBackdropMouseUp,
      onModalPanelMouseUp,
      unitStatus,
      unitStage,
      isInFlight,
      sessionBadgeTitle,
      healthHeadline,
      healthMark,
      healthTooltip,
      cardPrimary,
      cardNodeId,
      cardShowNodeId,
      aliasDraft,
      notesDraft,
      saveAlias,
      saveNotes,
      onAliasFocus: () => {
        aliasEditing.value = true
      },
      onNotesFocus: () => {
        notesEditing.value = true
      },
      formatAgo,
      formatRelative,
      formatSite,
      formatBattery,
      formatCount,
      formatPollWindow,
      formatUnreadableRf,
      formatTemp,
      formatUptime,
      formatRssi,
      formatSnr,
      hasHealthIssues,
      hasTrafficStats,
      healthTooltip,
      METRIC_ROWS,
      unitHistory,
      historyHours,
      sparkModels,
      sparkFallback,
      metricNow,
      formatPollDelta,
      formatPollVoltage,
      formatPollTemp,
      voltageStock,
      tempStock,
      logSlice,
      logHasMore,
      loadMoreLog,
      sunEmoji,
      sunElev,
      sunTitle,
      unitLabel,
      unitTitle,
      togglePublic,
      togglePaused,
      runManualJob,
      otaLocalLabel,
      otaHwLabel,
      otaTargetLabel,
      otaBodyLabel,
      otaServingLabel,
      otaBlLabel,
      canInstallUnit,
      canStageRow,
      runStage,
      runInstall,
      consoleMode,
      consoleTranscript,
      consoleDraft,
      consoleScroll,
      consoleBusy,
      consoleStatusLine,
      consoleLive,
      openConsoleKey,
      startConsole,
      exitConsole,
      clearConsoleHistory,
      submitConsoleLine,
      cancelConsoleSend,
    }
  },
  template: `
    <header id="header">
      <div class="brand">
        <h1>FleetEnvy</h1>
      </div>
      <div class="search-wrap">
        <label class="search-field">
          <span class="search-icon" aria-hidden="true">
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75">
              <circle cx="8.5" cy="8.5" r="5.25" />
              <path d="M13 13l3.5 3.5" stroke-linecap="round" />
            </svg>
          </span>
          <input
            id="search"
            v-model="search"
            class="search-input"
            type="search"
            placeholder="Search site, unit, name…"
            autocomplete="off"
            spellcheck="false"
          />
        </label>
      </div>
    </header>
    <div id="layout">
      <main id="map-wrap">
        <div id="map"></div>
        <div
          v-if="selectedUnit"
          class="detail-modal"
          :class="{ 'detail-console-mode': consoleMode }"
          @mousedown.self="onModalBackdropMouseDown"
          @mouseup.self="onModalBackdropMouseUp"
        >
          <div
            id="detail"
            class="detail"
            :class="{ 'detail-console': consoleMode }"
            role="dialog"
            aria-modal="true"
            aria-labelledby="detail-title"
            @mouseup="onModalPanelMouseUp"
          >
          <div class="detail-head">
            <h2 id="detail-title">{{ unitTitle(selectedUnit) }}</h2>
            <button type="button" class="detail-close" aria-label="Close" @click="clearSelection">×</button>
          </div>
          <p v-if="!consoleMode" class="sub">
            {{ selectedUnit.unit_id }}
            · {{ selectedUnit.public ? 'public' : 'private' }}
            · {{ formatRelative(selectedUnit.last_heard, fleet.now) }}
            <span v-if="selectedUnit.drift"> · {{ selectedUnit.drift }}</span>
          </p>
          <div class="detail-toolbar">
            <label
              class="book-toggle"
              title="Push site name and GPS when public"
            >
              <input type="checkbox" :checked="!!selectedUnit.public" @change="togglePublic(selectedUnit, $event)" />
              <span class="switch" aria-hidden="true"></span>
              Public
            </label>
            <label
              class="book-toggle book-toggle-pause"
              title="Skip auto poll and apply. Refresh, Pull, and Push still work."
            >
              <input type="checkbox" :checked="!!selectedUnit.paused" @change="togglePaused(selectedUnit, $event)" />
              <span class="switch" aria-hidden="true"></span>
              Pause
            </label>
            <div v-if="manualAccepting" class="manual-actions">
              <button
                v-if="!consoleMode"
                type="button"
                class="manual-btn manual-btn-console"
                @click="startConsole(selectedUnit)"
              >
                Console
              </button>
              <button
                v-else
                type="button"
                class="manual-btn manual-btn-console-exit"
                @click="exitConsole"
              >
                Exit console
              </button>
              <button
                type="button"
                class="manual-btn"
                :disabled="!canManualUnit(selectedUnit, 'refresh')"
                @click="runManualJob(selectedUnit, 'refresh', $event)"
              >
                Refresh
              </button>
              <button
                type="button"
                class="manual-btn"
                :disabled="!canManualUnit(selectedUnit, 'pull')"
                @click="runManualJob(selectedUnit, 'pull', $event)"
              >
                Pull
              </button>
              <button
                type="button"
                class="manual-btn manual-btn-push"
                :disabled="!canManualUnit(selectedUnit, 'push')"
                @click="runManualJob(selectedUnit, 'push', $event)"
              >
                Push
              </button>
            </div>
          </div>
          <section v-if="consoleMode" class="console-panel">
            <div class="console-toolbar">
              <button
                type="button"
                class="console-clear"
                :disabled="!consoleTranscript.length"
                @click="clearConsoleHistory"
              >
                Clear history
              </button>
            </div>
            <div ref="consoleScroll" class="console-transcript">
              <div v-for="(row, i) in consoleTranscript" :key="i" class="console-block">
                <div class="console-cmd">&gt; {{ row.cmd }}</div>
                <div v-if="row.pending && consoleStatusLine" class="console-status">
                  {{ consoleStatusLine }}
                  <button type="button" class="console-cancel" @click="cancelConsoleSend">Cancel</button>
                </div>
                <pre v-if="row.reply" class="console-reply">{{ row.reply }}</pre>
                <pre v-if="row.error" class="console-error">{{ row.error }}</pre>
              </div>
              <div v-if="!consoleTranscript.length && consoleStatusLine" class="console-status console-status-empty">
                {{ consoleStatusLine }}
              </div>
            </div>
            <form class="console-input-row" @submit.prevent="submitConsoleLine">
              <input
                v-model="consoleDraft"
                class="console-input"
                type="text"
                placeholder="ota stats"
                autocomplete="off"
                spellcheck="false"
                :disabled="consoleBusy || consoleLive?.state === 'acquiring'"
              />
              <button type="submit" class="manual-btn" :disabled="consoleBusy || !consoleDraft.trim()">Send</button>
            </form>
          </section>
          <template v-if="!consoleMode">
          <section class="book-edit">
            <label class="book-field">
              <span class="book-field-label">Alias</span>
              <input
                v-model="aliasDraft"
                class="book-input"
                type="text"
                placeholder="Bag label when no site"
                autocomplete="off"
                spellcheck="false"
                @focus="onAliasFocus"
                @blur="selectedUnit && saveAlias(selectedUnit)"
              />
            </label>
            <label class="book-field">
              <span class="book-field-label">Notes</span>
              <textarea
                v-model="notesDraft"
                class="book-textarea"
                rows="3"
                placeholder="Bench notes, deployment history…"
                spellcheck="true"
                @focus="onNotesFocus"
                @blur="selectedUnit && saveNotes(selectedUnit)"
              ></textarea>
            </label>
          </section>
          <section v-if="selectedUnit.health" class="health-section">
            <div class="health-summary">
              <span
                class="health-chip"
                :class="'health-' + healthHeadline(selectedUnit)"
              >
                {{ healthMark(selectedUnit) }}
              </span>
            </div>
            <ul v-if="hasHealthIssues(selectedUnit.health)" class="health-issues">
              <li
                v-for="(issue, i) in selectedUnit.health.issues"
                :key="i"
                :class="'health-' + issue.status"
              >
                <div>{{ issue.name }}: {{ issue.reason || issue.status }}</div>
                <div v-if="issue.fix" class="health-fix">{{ issue.fix }}</div>
              </li>
            </ul>
            <div class="metric-grid">
              <div v-for="row in METRIC_ROWS" :key="row.key" class="metric-row">
                <span class="metric-label">{{ row.label }}</span>
                <span class="metric-now">{{ metricNow(row.key) }}</span>
                <svg
                  v-if="sparkModels[row.key]"
                  class="spark"
                  width="168"
                  height="22"
                  viewBox="0 0 168 22"
                  aria-hidden="true"
                >
                  <line
                    v-if="row.wave && sparkModels[row.key].zeroY != null"
                    class="spark-horizon"
                    x1="2"
                    x2="166"
                    :y1="sparkModels[row.key].zeroY"
                    :y2="sparkModels[row.key].zeroY"
                  />
                  <polyline
                    v-if="sparkModels[row.key].line"
                    fill="none"
                    :stroke="row.stroke"
                    :stroke-width="row.wave ? 1.7 : 1.5"
                    :points="sparkModels[row.key].line"
                  />
                  <circle
                    v-for="(dot, di) in sparkModels[row.key].dots"
                    :key="di"
                    :cx="dot.x"
                    :cy="dot.y"
                    r="1.6"
                    :fill="dot.synthetic ? 'none' : row.stroke"
                    :stroke="row.stroke"
                    :stroke-width="dot.synthetic ? 1.2 : 0"
                    :opacity="dot.synthetic ? 0.65 : 1"
                  />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback(row.key) }}</span>
              </div>
            </div>
          </section>
          <section class="ota-section">
            <h3>Firmware</h3>
            <dl>
              <dt>Version</dt>
              <dd>
                {{ selectedUnit.firmware_version || '—' }}
                <span v-if="selectedUnit.firmware_platform" class="dim">{{
                  selectedUnit.firmware_platform
                }}</span>
              </dd>
              <dt v-if="selectedUnit.ota">HW</dt>
              <dd v-if="selectedUnit.ota">{{ otaHwLabel(selectedUnit.ota.running) }}</dd>
              <dt v-if="selectedUnit.ota">Target</dt>
              <dd v-if="selectedUnit.ota">{{ otaTargetLabel(selectedUnit.ota.running) }}</dd>
              <dt v-if="selectedUnit.base_hash || selectedUnit.ota?.running?.body_hash">Body</dt>
              <dd v-if="selectedUnit.base_hash || selectedUnit.ota?.running?.body_hash">
                {{ otaBodyLabel(selectedUnit) }}
              </dd>
              <dt v-if="selectedUnit.bootloader_version || selectedUnit.ota?.running?.bl_apply != null">
                Bootloader
              </dt>
              <dd v-if="selectedUnit.bootloader_version || selectedUnit.ota?.running?.bl_apply != null">
                {{ selectedUnit.bootloader_version || otaBlLabel(selectedUnit.ota?.running) }}
                <span
                  v-if="selectedUnit.ota?.running?.bl_apply === true"
                  class="ota-tag ota-tag-ok"
                  title="Bootloader can apply in-place"
                >apply</span>
                <span
                  v-else-if="selectedUnit.ota?.running?.bl_apply === false"
                  class="ota-tag ota-tag-warn"
                  title="Bootloader cannot apply OTA"
                >no apply</span>
              </dd>
            </dl>
            <template v-if="selectedUnit.ota || manualAccepting">
              <h3>OTA</h3>
              <dl>
                <dt>Serving</dt>
                <dd>{{ otaServingLabel(selectedUnit.ota?.running) }}</dd>
                <dt>Keys</dt>
                <dd>
                  {{ selectedUnit.ota?.running?.keys != null ? selectedUnit.ota.running.keys : '—' }}
                </dd>
                <template v-if="selectedUnit.ota?.running && 'seed' in selectedUnit.ota.running">
                  <dt>Seeder</dt>
                  <dd>{{ selectedUnit.ota.running.seed ? 'on' : 'off' }}</dd>
                </template>
                <dt>Local</dt>
                <dd>{{ otaLocalLabel(selectedUnit.ota) }}</dd>
                <dt>Heard (yours)</dt>
                <dd>
                  <ul v-if="selectedUnit.ota?.heard_yours?.length" class="ota-heard-list">
                    <li v-for="row in selectedUnit.ota.heard_yours" :key="row.index">
                      <span class="ota-heard-main"
                        >#{{ row.index }} {{ row.version }} {{ row.codec }}
                        <span class="dim">{{ row.n_seeders }}n {{ row.age_s }}s</span></span
                      >
                      <button
                        v-if="manualAccepting"
                        type="button"
                        class="ota-stage-btn"
                        :disabled="!canStageRow(selectedUnit, row)"
                        @click="runStage(selectedUnit, row, $event)"
                      >
                        Stage
                      </button>
                    </li>
                  </ul>
                  <template v-else>none seen (Refresh to re-poll)</template>
                </dd>
              </dl>
            </template>
            <div v-if="manualAccepting" class="ota-actions">
              <button
                type="button"
                class="manual-btn manual-btn-install"
                :disabled="!canInstallUnit(selectedUnit)"
                @click="runInstall(selectedUnit, $event)"
              >
                Install
              </button>
            </div>
          </section>
          <section>
            <dl>
              <dt>GPS</dt>
              <dd>
                <template v-if="selectedUnit.position">
                  {{ selectedUnit.position.lat.toFixed(5) }}, {{ selectedUnit.position.lon.toFixed(5) }}
                </template>
                <template v-else>unmapped</template>
              </dd>
              <dt>Uptime</dt>
              <dd>{{ formatUptime(selectedUnit.status?.uptime_secs) }}</dd>
            </dl>
          </section>
          <section v-if="hasTrafficStats(selectedUnit.status)">
            <h3>Since boot</h3>
            <dl>
              <dt>In / out</dt>
              <dd>
                {{ formatCount(selectedUnit.status?.packets_recv) }} /
                {{ formatCount(selectedUnit.status?.packets_sent) }}
              </dd>
              <template v-if="selectedUnit.status?.recv_errors != null">
                <dt>Unreadable</dt>
                <dd>
                  {{ formatUnreadableRf(selectedUnit.status?.recv_errors, selectedUnit.status?.packets_recv) }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.status?.recv_flood != null ||
                  selectedUnit.status?.recv_direct != null
                "
              >
                <dt>Recv F / D</dt>
                <dd>
                  {{ formatCount(selectedUnit.status?.recv_flood) }} /
                  {{ formatCount(selectedUnit.status?.recv_direct) }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.status?.sent_flood != null ||
                  selectedUnit.status?.sent_direct != null
                "
              >
                <dt>Sent F / D</dt>
                <dd>
                  {{ formatCount(selectedUnit.status?.sent_flood) }} /
                  {{ formatCount(selectedUnit.status?.sent_direct) }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.status?.last_snr != null ||
                  selectedUnit.status?.last_rssi != null
                "
              >
                <dt>SNR / RSSI</dt>
                <dd>
                  {{ formatSnr(selectedUnit.status?.last_snr) }} /
                  {{ formatRssi(selectedUnit.status?.last_rssi) }}
                </dd>
              </template>
            </dl>
          </section>
          <section v-if="selectedUnit.neighbors?.length">
            <h3>Neighbors · {{ selectedUnit.neighbors.filter(n => n.unit_key).length }}</h3>
            <div
              v-for="(nb, i) in selectedUnit.neighbors.filter(n => n.unit_key)"
              :key="i"
              class="neighbor-row"
            >
              {{ nb.label || nb.unit_id || nb.pubkey_prefix }}
              · {{ nb.snr ?? '?' }} dB
              · {{ formatAgo(nb.secs_ago) }}
            </div>
          </section>
          <section v-if="unitHistory?.status?.length" class="poll-log-section">
            <div class="poll-log">
              <h3>Status · {{ unitHistory.status.length }}</h3>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Sun</th>
                    <th>V</th>
                    <th>In / out</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(row, pi) in logSlice('status')"
                    :key="'st-' + pi"
                    :class="{ 'poll-reboot': row.reboot }"
                  >
                    <td>{{ formatRelative(row.ts, fleet.now) }}</td>
                    <td>
                      <span
                        v-if="sunEmoji(row.sun)"
                        class="poll-sun"
                        :title="sunTitle(row.sun)"
                      >
                        {{ sunEmoji(row.sun) }}
                        <span v-if="sunElev(row.sun)" class="poll-sun-elev">{{
                          sunElev(row.sun)
                        }}</span>
                      </span>
                      <template v-else>—</template>
                    </td>
                    <td class="poll-metric">
                      {{ formatPollVoltage(row) }}
                      <span
                        v-if="voltageStock(row)"
                        class="stock-delta"
                        :class="'stock-' + voltageStock(row).dir"
                      >{{ voltageStock(row).text }}</span>
                    </td>
                    <td>{{ formatPollDelta(row.delta_packets_recv, row.delta_packets_sent) }}</td>
                    <td>
                      <template v-if="row.reboot">reboot</template>
                      <template v-else-if="row.since_prev_secs != null">{{
                        formatPollWindow(row.since_prev_secs)
                      }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
              </table>
              <button
                v-if="logHasMore('status')"
                type="button"
                class="load-more"
                @click="loadMoreLog('status')"
              >
                Load more
              </button>
            </div>
          </section>
          <section v-if="unitHistory?.telemetry?.length" class="poll-log-section">
            <div class="poll-log">
              <h3>Telemetry · {{ unitHistory.telemetry.length }}</h3>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Sun</th>
                    <th>Temp</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, pi) in logSlice('telemetry')" :key="'te-' + pi">
                    <td>{{ formatRelative(row.ts, fleet.now) }}</td>
                    <td>
                      <span
                        v-if="sunEmoji(row.sun)"
                        class="poll-sun"
                        :title="sunTitle(row.sun)"
                      >
                        {{ sunEmoji(row.sun) }}
                        <span v-if="sunElev(row.sun)" class="poll-sun-elev">{{
                          sunElev(row.sun)
                        }}</span>
                      </span>
                      <template v-else>—</template>
                    </td>
                    <td class="poll-metric">
                      {{ formatPollTemp(row) }}
                      <span
                        v-if="tempStock(row)"
                        class="stock-delta"
                        :class="'stock-' + tempStock(row).dir"
                      >{{ tempStock(row).text }}</span>
                    </td>
                    <td>
                      <template v-if="row.since_prev_secs != null">{{
                        formatPollWindow(row.since_prev_secs)
                      }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
              </table>
              <button
                v-if="logHasMore('telemetry')"
                type="button"
                class="load-more"
                @click="loadMoreLog('telemetry')"
              >
                Load more
              </button>
            </div>
          </section>
          <section v-if="unitHistory?.neighbors?.length" class="poll-log-section">
            <div class="poll-log">
              <h3>Neighbors · {{ unitHistory.neighbors.length }}</h3>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Count</th>
                    <th>Δ</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, pi) in logSlice('neighbors')" :key="'nb-' + pi">
                    <td>{{ formatRelative(row.ts, fleet.now) }}</td>
                    <td>{{ row.count ?? '—' }}</td>
                    <td>{{ row.delta_count != null ? (row.delta_count >= 0 ? '+' : '') + row.delta_count : '—' }}</td>
                    <td>
                      <template v-if="row.since_prev_secs != null">{{
                        formatPollWindow(row.since_prev_secs)
                      }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
              </table>
              <button
                v-if="logHasMore('neighbors')"
                type="button"
                class="load-more"
                @click="loadMoreLog('neighbors')"
              >
                Load more
              </button>
            </div>
          </section>
          <section v-if="unitHistory?.acl?.length" class="poll-log-section">
            <div class="poll-log">
              <h3>ACL · {{ unitHistory.acl.length }}</h3>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Keys</th>
                    <th>Δ</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, pi) in logSlice('acl')" :key="'ac-' + pi">
                    <td>{{ formatRelative(row.ts, fleet.now) }}</td>
                    <td>{{ row.count ?? '—' }}</td>
                    <td>{{ row.delta_count != null ? (row.delta_count >= 0 ? '+' : '') + row.delta_count : '—' }}</td>
                    <td>
                      <template v-if="row.since_prev_secs != null">{{
                        formatPollWindow(row.since_prev_secs)
                      }}</template>
                      <template v-else>—</template>
                    </td>
                  </tr>
                </tbody>
              </table>
              <button
                v-if="logHasMore('acl')"
                type="button"
                class="load-more"
                @click="loadMoreLog('acl')"
              >
                Load more
              </button>
            </div>
          </section>
          </template>
          </div>
        </div>
      </main>
      <aside id="sidebar">
        <div class="sidebar-toolbar">
          <div class="list-filter" role="tablist" aria-label="List filter">
            <button
              type="button"
              role="tab"
              :class="{ on: listFilter === 'all' }"
              :aria-selected="listFilter === 'all'"
              @click="listFilter = 'all'"
            >
              All
            </button>
            <button
              type="button"
              role="tab"
              :class="{ on: listFilter === 'active' }"
              :aria-selected="listFilter === 'active'"
              @click="listFilter = 'active'"
            >
              Active{{ activeCount ? ' ' + activeCount : '' }}
            </button>
          </div>
        </div>
        <div id="unit-list">
          <p v-if="!sortedUnits.length" class="list-empty">
            {{ listFilter === 'active' ? 'Nothing in flight.' : 'No units.' }}
          </p>
          <div
            v-for="unit in sortedUnits"
            :key="unit.key"
            class="unit-card"
            :class="{ selected: unit.key === selectedKey, paused: !!unit.paused, busy: isInFlight(unit) }"
            @click="selectUnit(unit.key)"
          >
            <div class="unit-row1">
              <span class="unit-title" :title="healthTooltip(unit.health)">
                <span class="unit-name" :class="'unit-name-' + healthHeadline(unit)">{{ cardPrimary(unit) }}</span>
                <span class="unit-meta">
                  <span v-if="cardShowNodeId(unit)" class="unit-id">{{ cardNodeId(unit) }}</span>
                  <span v-if="unit.ota_badge" class="ota-list-badge" :class="'ota-badge-' + unit.ota_badge.replace(' ', '-')">{{
                    unit.ota_badge
                  }}</span>
                  <span class="unit-ago">{{ formatRelative(unit.last_heard, fleet.now) }}</span>
                </span>
              </span>
              <button
                v-if="manualAccepting"
                type="button"
                class="unit-refresh"
                :class="{ spinning: isInFlight(unit) }"
                :disabled="!canManualUnit(unit, 'refresh')"
                :title="isInFlight(unit) ? unitStage(unit) : 'Refresh'"
                :aria-label="isInFlight(unit) ? unitStage(unit) : 'Refresh'"
                @click.stop="runManualJob(unit, 'refresh', $event)"
              >
                <span class="unit-refresh-icon" aria-hidden="true">↻</span>
              </button>
            </div>
            <Transition name="stage">
              <div
                v-if="isInFlight(unit)"
                class="unit-stage"
                :title="sessionBadgeTitle(unit)"
              >
                {{ unitStage(unit) }}
              </div>
            </Transition>
          </div>
        </div>
      </aside>
    </div>
  `,
}

createApp(App).mount('#app')
