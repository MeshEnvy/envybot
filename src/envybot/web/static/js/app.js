import { createApp, computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { connectEvents, fetchFleet, fetchPolls, patchUnit, pullUnit, pushUnit, refreshUnit } from './api.js'
import {
  clearHistories,
  fleetStore,
  loadHistories,
  patchSession,
  patchUnit as storePatchUnit,
  replaceSnapshot,
} from './state.js?v=1'
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
  hasHealthIssues,
  hasTrafficStats,
  healthHeadline,
  healthMark,
  healthTooltip,
  compareUnits,
  unitLabel,
  unitTitle,
} from './format.js?v=18'
import { buildNeighborEdges, createMapController, unitStage, unitStatus } from './map.js?v=22'
import { seriesFromHistories, sparklineWallTime, SPARK_MIN_SPAN } from './sparklines.js?v=6'

const App = {
  setup() {
    const fleet = fleetStore
    const selectedKey = ref(null)
    const search = ref('')
    const listFilter = ref('all')
    const historyHours = 72

    const METRIC_ROWS = [
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

    const MANUAL_BUSY = new Set(['queued', 'refreshing', 'pulling', 'pushing', 'polling'])

    /** @param {Record<string, unknown> | undefined} unit */
    function isInFlight(unit) {
      const s = unit?.session
      const state = s && typeof s === 'object' && 'state' in s ? s.state : null
      return MANUAL_BUSY.has(state)
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function canManualUnit(unit) {
      if (!unit || !manualAccepting.value) return false
      return !isInFlight(unit)
    }

    /** @param {Record<string, unknown>} unit @param {'refresh' | 'pull' | 'push'} job */
    function markOptimistic(unit, job) {
      const key = String(unit.key)
      const prev = fleet.units[key] || unit
      const state = job === 'refresh' ? 'refreshing' : job === 'pull' ? 'pulling' : 'pushing'
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
      if (!canManualUnit(unit)) return
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

    const activeCount = computed(() => Object.values(fleet.units || {}).filter(isInFlight).length)

    const sortedUnits = computed(() => {
      const q = search.value.trim().toLowerCase()
      let units = Object.values(fleet.units || {})
      if (listFilter.value === 'active') {
        units = units.filter(isInFlight)
      }
      if (q) {
        units = units.filter((u) => {
          const hay = [u.key, u.unit_id, u.site, u.site_name, u.label]
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

    const sparkModels = computed(() => {
      /** @type {Record<string, ReturnType<typeof sparklineWallTime>>} */
      const out = {}
      for (const row of METRIC_ROWS) {
        out[row.key] = sparklineWallTime(sparkSeries.value[row.key] || [], 168, 22, {
          minSpan: SPARK_MIN_SPAN[row.key] || 0,
        })
      }
      return out
    })

    const SPARK_VALUE_FORMAT = {
      battery_mv: (v) => `${v.toFixed(2)} V`,
      temperature: (v) => `${v.toFixed(1)} °C`,
      unreadable_pct: (v) => `${v.toFixed(1)}%`,
      recv_rate: (v) => `${v.toFixed(1)}/h`,
      noise_floor: (v) => `${Math.trunc(v)} dBm`,
    }

    function metricNow(metric) {
      const unit = selectedUnit.value
      if (metric === 'battery_mv') {
        const mv = unit?.status?.battery_mv
        if (mv != null) return formatBattery(mv)
        const v = unit?.telemetry?.voltage
        if (v != null) return `${Number(v).toFixed(2)} V`
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
      if (poll?.voltage != null) return `${Number(poll.voltage).toFixed(2)} V`
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
      return formatSignedDelta(poll?.delta_temperature, 1)
    }

    function selectUnit(key) {
      if (selectedKey.value === key) {
        clearSelection()
        return
      }
      selectedKey.value = key
      mapCtrl?.flyTo(key, fleet)
      mapCtrl?.sync(fleet, key)
      loadHistoriesFor(key)
    }

    function clearSelection() {
      selectedKey.value = null
      mapCtrl?.sync(fleet, null)
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
      if (unit.site_name) return unit.site_name
      return String(unit.unit_id || unit.key || '')
    }

    function cardNodeId(unit) {
      return String(unit.unit_id || unit.key || '')
    }

    function cardShowNodeId(unit) {
      const id = cardNodeId(unit)
      return !!unit.site_name && !!id && id !== cardPrimary(unit)
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
      mapCtrl?.sync(fleet, selectedKey.value)
    }

    onMounted(async () => {
      // Let Vue finish layout so #map has non-zero size before MapLibre inits.
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
      mapCtrl = createMapController('map', selectUnit, clearSelection)
      const onKey = (e) => {
        if (e.key === 'Escape') clearSelection()
      }
      window.addEventListener('keydown', onKey)
      onUnmounted(() => window.removeEventListener('keydown', onKey))
      try {
        applyHello(await fetchFleet())
      } catch (err) {
        console.error(err)
      }
      pushMap()
      // Second sync after layout/tiles settle.
      requestAnimationFrame(() => pushMap())
      es = connectEvents({
        onHello: (snap) => {
          applyHello(snap)
          pushMap()
        },
        onUnit: (unit) => {
          applyUnit(unit)
          pushMap()
        },
        onSession: (poll) => {
          applySession(poll)
        },
      })
    })

    watch(search, () => {})

    onUnmounted(() => {
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
      unitLabel,
      unitTitle,
      togglePublic,
      togglePaused,
      runManualJob,
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
          @click.self="clearSelection"
        >
          <div
            id="detail"
            class="detail"
            role="dialog"
            aria-modal="true"
            aria-labelledby="detail-title"
          >
          <div class="detail-head">
            <h2 id="detail-title">{{ unitTitle(selectedUnit) }}</h2>
            <button type="button" class="detail-close" aria-label="Close" @click="clearSelection">×</button>
          </div>
          <p class="sub">
            {{ selectedUnit.unit_id }}
            · {{ selectedUnit.public ? 'public' : 'private' }}
            · {{ formatRelative(selectedUnit.last_heard) }}
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
                type="button"
                class="manual-btn"
                :disabled="!canManualUnit(selectedUnit)"
                @click="runManualJob(selectedUnit, 'refresh', $event)"
              >
                Refresh
              </button>
              <button
                type="button"
                class="manual-btn"
                :disabled="!canManualUnit(selectedUnit)"
                @click="runManualJob(selectedUnit, 'pull', $event)"
              >
                Pull
              </button>
              <button
                type="button"
                class="manual-btn manual-btn-push"
                :disabled="!canManualUnit(selectedUnit)"
                @click="runManualJob(selectedUnit, 'push', $event)"
              >
                Push
              </button>
            </div>
          </div>
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
                  <polyline
                    v-if="sparkModels[row.key].line"
                    fill="none"
                    :stroke="row.stroke"
                    stroke-width="1.5"
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
          <section>
            <dl>
              <dt>Firmware</dt>
              <dd>
                {{ selectedUnit.firmware_version || '—' }}
                <span v-if="selectedUnit.firmware_platform" class="dim">{{
                  selectedUnit.firmware_platform
                }}</span>
              </dd>
              <dt v-if="selectedUnit.bootloader_version">Bootloader</dt>
              <dd v-if="selectedUnit.bootloader_version">{{ selectedUnit.bootloader_version }}</dd>
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
            <details class="poll-log" open>
              <summary>Status · {{ unitHistory.status.length }}</summary>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>V</th>
                    <th>In / out</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(row, pi) in unitHistory.status"
                    :key="'st-' + pi"
                    :class="{ 'poll-reboot': row.reboot }"
                  >
                    <td>{{ formatRelative(row.ts) }}</td>
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
            </details>
          </section>
          <section v-if="unitHistory?.telemetry?.length" class="poll-log-section">
            <details class="poll-log">
              <summary>Telemetry · {{ unitHistory.telemetry.length }}</summary>
              <table class="poll-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>V</th>
                    <th>Temp</th>
                    <th>Gap</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, pi) in unitHistory.telemetry" :key="'te-' + pi">
                    <td>{{ formatRelative(row.ts) }}</td>
                    <td class="poll-metric">
                      {{ row.voltage != null ? Number(row.voltage).toFixed(2) + ' V' : '—' }}
                      <span
                        v-if="voltageStock(row)"
                        class="stock-delta"
                        :class="'stock-' + voltageStock(row).dir"
                      >{{ voltageStock(row).text }}</span>
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
            </details>
          </section>
          <section v-if="unitHistory?.neighbors?.length" class="poll-log-section">
            <details class="poll-log">
              <summary>Neighbors · {{ unitHistory.neighbors.length }}</summary>
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
                  <tr v-for="(row, pi) in unitHistory.neighbors" :key="'nb-' + pi">
                    <td>{{ formatRelative(row.ts) }}</td>
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
            </details>
          </section>
          <section v-if="unitHistory?.acl?.length" class="poll-log-section">
            <details class="poll-log">
              <summary>ACL · {{ unitHistory.acl.length }}</summary>
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
                  <tr v-for="(row, pi) in unitHistory.acl" :key="'ac-' + pi">
                    <td>{{ formatRelative(row.ts) }}</td>
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
            </details>
          </section>
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
                  <span class="unit-ago">{{ formatRelative(unit.last_heard) }}</span>
                </span>
              </span>
              <button
                v-if="manualAccepting"
                type="button"
                class="unit-refresh"
                :class="{ spinning: isInFlight(unit) }"
                :disabled="!canManualUnit(unit)"
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
