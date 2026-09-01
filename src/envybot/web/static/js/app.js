import { createApp, computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { connectEvents, fetchFleet, fetchHistory, patchUnit, pullUnit, pushUnit, refreshUnit } from './api.js'
import {
  formatAgo,
  formatAirtimePct,
  formatBattery,
  formatCount,
  formatNoiseFloor,
  formatPollWindow,
  formatUnreadableRf,
  formatRelative,
  formatRssi,
  formatSite,
  formatSnr,
  formatTemp,
  formatUptime,
  hasHealthIssues,
  hasTrafficInterval,
  hasTrafficStats,
  healthEmoji,
  healthHeadline,
  healthMark,
  healthTooltip,
  compareUnits,
  unitLabel,
  unitTitle,
} from './format.js?v=14'
import { buildNeighborEdges, createMapController, hasMapPin, unitStage, unitStatus } from './map.js?v=18'
import { healthStroke, sparklinePath } from './sparklines.js?v=1'

const App = {
  setup() {
    const fleet = reactive({
      units: {},
      counts: {},
      poll: {},
      companion: null,
      edges: [],
    })
    const selectedKey = ref(null)
    const search = ref('')
    const listFilter = ref('all')
    const sparkData = reactive({
      voltage: [],
      temperature: [],
      unreadable_pct: [],
      recv_rate: [],
      noise_floor: [],
    })
    /** @type {import('vue').Ref<HTMLElement | null>} */
    const detailEl = ref(null)
    /** @type {ReturnType<typeof createMapController> | null} */
    let mapCtrl = null
    /** @type {EventSource | null} */
    let es = null

    function applyHello(snap) {
      fleet.units = snap.units || {}
      fleet.edges = edgeList(snap.edges)
      fleet.counts = snap.counts || {}
      fleet.poll = { ...(snap.poll || {}) }
      if (snap.companion != null) fleet.companion = snap.companion
    }

    /** @param {unknown} value */
    function edgeList(value) {
      return Array.isArray(value) ? value : []
    }

    /** @param {Record<string, unknown>} unit */
    function applyUnit(unit) {
      const key = String(unit.key)
      fleet.units[key] = { ...(fleet.units[key] || {}), ...unit }
      fleet.edges = buildNeighborEdges(
        /** @type {Record<string, Record<string, unknown>>} */ (fleet.units)
      )
    }

    /** @param {Record<string, unknown>} poll */
    function applySession(poll) {
      Object.assign(fleet.poll, poll)
      if (poll.companion != null) fleet.companion = poll.companion
    }

    const pollLine = computed(() => {
      const p = fleet.poll || {}
      const parts = [p.phase || 'idle']
      if (p.round) parts.push(`round ${p.round}`)
      if (p.pending != null) parts.push(`${p.pending} pending`)
      if (fleet.companion) parts.push(`companion ${String(fleet.companion).slice(0, 12)}…`)
      return parts.join(' · ')
    })

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

    const detailHasMapPin = computed(() => hasMapPin(selectedUnit.value?.position))

    function restowDetailEl() {
      const el = detailEl.value
      if (!el) return
      const wrap = document.getElementById('map-wrap')
      if (wrap && el.parentElement !== wrap) wrap.appendChild(el)
    }

    function syncDetailPopup() {
      const unit = selectedUnit.value
      const el = detailEl.value
      if (!unit || !el) {
        mapCtrl?.detachDetail()
        restowDetailEl()
        return
      }
      if (hasMapPin(unit.position)) {
        const lat = Number(unit.position.lat)
        const lon = Number(unit.position.lon)
        mapCtrl.attachDetail(el, [lon, lat])
        return
      }
      mapCtrl?.detachDetail()
      restowDetailEl()
    }

    async function loadSparklines(key) {
      const metrics = ['voltage', 'temperature', 'unreadable_pct', 'recv_rate', 'noise_floor']
      const results = await Promise.all(
        metrics.map((m) =>
          fetchHistory(key, m)
            .then((r) => r.points || [])
            .catch(() => [])
        )
      )
      metrics.forEach((m, i) => {
        sparkData[m] = results[i]
      })
    }

    function sparkPath(metric) {
      return sparklinePath(sparkData[metric] || [], 120, 28)
    }

    const SPARK_VALUE_FORMAT = {
      voltage: (v) => `${v.toFixed(2)} V`,
      temperature: (v) => `${v.toFixed(1)} °C`,
      unreadable_pct: (v) => `${v.toFixed(1)}%`,
      recv_rate: (v) => `${v.toFixed(1)}/h`,
      noise_floor: (v) => `${Math.trunc(v)} dBm`,
    }

    /** Latest value as text when there are too few points for a line. */
    function sparkFallback(metric) {
      const values = (sparkData[metric] || [])
        .map((p) => p.value)
        .filter((v) => v != null && Number.isFinite(Number(v)))
      if (!values.length) return '—'
      const fmt = SPARK_VALUE_FORMAT[metric] || ((v) => String(v))
      return `${fmt(Number(values[values.length - 1]))} · 1 poll`
    }

    async function selectUnit(key) {
      if (selectedKey.value === key) {
        clearSelection()
        return
      }
      selectedKey.value = key
      mapCtrl?.flyTo(key, fleet)
      mapCtrl?.sync(fleet, key)
      loadSparklines(key)
      await nextTick()
      syncDetailPopup()
    }

    function clearSelection() {
      selectedKey.value = null
      sparkData.voltage = []
      sparkData.temperature = []
      sparkData.unreadable_pct = []
      sparkData.recv_rate = []
      sparkData.noise_floor = []
      mapCtrl?.detachDetail()
      restowDetailEl()
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

    function cardTitle(unit) {
      if (unit.site_name) return unit.site_name
      return String(unit.unit_id || unit.key || '')
    }

    function cardMeta(unit) {
      const parts = []
      if (unit.site_name) {
        parts.push(unit.unit_id || unit.key)
      } else if (!unit.site) {
        parts.push(formatSite(unit.site))
      }
      if (unit.firmware_version) parts.push(`fw ${unit.firmware_version}`)
      const bat = unit.status?.battery_mv
      if (bat != null) parts.push(formatBattery(bat))
      parts.push(formatRelative(unit.last_heard))
      if (!unit.mapped) parts.push('no map pin')
      return parts.join(' · ')
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
      syncDetailPopup()
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
      detailEl,
      detailHasMapPin,
      pollLine,
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
      healthEmoji,
      healthMark,
      cardTitle,
      cardMeta,
      formatAgo,
      formatRelative,
      formatSite,
      formatAirtimePct,
      formatBattery,
      formatCount,
      formatNoiseFloor,
      formatPollWindow,
      formatUnreadableRf,
      formatTemp,
      formatUptime,
      formatRssi,
      formatSnr,
      hasHealthIssues,
      hasTrafficInterval,
      hasTrafficStats,
      healthStroke,
      healthTooltip,
      sparkPath,
      sparkFallback,
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
        <h1>EnvyBot Fleet</h1>
        <p id="poll-line">{{ pollLine }}</p>
      </div>
      <div id="stats" class="stats">
        <span class="stat"><strong>{{ fleet.counts?.total ?? 0 }}</strong> units</span>
        <span class="stat"><strong>{{ fleet.counts?.mapped ?? 0 }}</strong> mapped</span>
        <span class="stat"><strong>{{ fleet.counts?.fresh ?? 0 }}</strong> fresh</span>
        <span class="stat"><strong>{{ fleet.counts?.stale ?? 0 }}</strong> stale</span>
        <span class="stat"><strong>{{ fleet.counts?.never ?? 0 }}</strong> never</span>
        <span class="stat"><strong>{{ fleet.counts?.paused ?? 0 }}</strong> paused</span>
      </div>
      <div class="search-wrap">
        <input v-model="search" type="search" placeholder="Search site, unit, name…" autocomplete="off" />
      </div>
    </header>
    <div id="layout">
      <main id="map-wrap">
        <div id="map"></div>
        <div
          v-if="selectedUnit"
          ref="detailEl"
          id="detail"
          class="detail"
          :class="{ 'detail-unmapped': !detailHasMapPin }"
        >
          <div class="detail-head">
            <h2>{{ unitTitle(selectedUnit) }}</h2>
            <button type="button" class="detail-close" aria-label="Close" @click="clearSelection">×</button>
          </div>
          <p class="sub">
            {{ selectedUnit.unit_id }}
            · {{ selectedUnit.public ? 'public' : 'private' }}
            · {{ isInFlight(selectedUnit) ? unitStage(selectedUnit) : healthMark(selectedUnit) }}
            · {{ formatRelative(selectedUnit.last_heard) }}
            <span v-if="selectedUnit.drift"> · {{ selectedUnit.drift }}</span>
          </p>
          <label class="book-toggle">
            <input type="checkbox" :checked="!!selectedUnit.public" @change="togglePublic(selectedUnit, $event)" />
            public (push site name + GPS)
          </label>
          <label class="book-toggle">
            <input type="checkbox" :checked="!!selectedUnit.paused" @change="togglePaused(selectedUnit, $event)" />
            pause auto poll/apply (Refresh, Pull, and Push still work)
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
          <section v-if="selectedUnit.health" class="health-section">
            <h3>Health</h3>
            <p class="health-summary">
              <span
                class="health-chip"
                :class="'health-' + healthHeadline(selectedUnit)"
              >
                {{ healthMark(selectedUnit) }}
              </span>
              <template v-if="!hasHealthIssues(selectedUnit.health)">
                {{ selectedUnit.health.summary }}
              </template>
            </p>
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
            <div class="spark-grid">
              <div class="spark-row">
                <span class="spark-label">Voltage</span>
                <svg v-if="sparkPath('voltage')" class="spark" width="120" height="28" viewBox="0 0 120 28" aria-hidden="true">
                  <polyline fill="none" stroke="#6ee7a0" stroke-width="1.5" :points="sparkPath('voltage')" />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback('voltage') }}</span>
              </div>
              <div class="spark-row">
                <span class="spark-label">Temp</span>
                <svg v-if="sparkPath('temperature')" class="spark" width="120" height="28" viewBox="0 0 120 28" aria-hidden="true">
                  <polyline fill="none" stroke="#f0b86e" stroke-width="1.5" :points="sparkPath('temperature')" />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback('temperature') }}</span>
              </div>
              <div class="spark-row">
                <span class="spark-label">Unreadable %</span>
                <svg v-if="sparkPath('unreadable_pct')" class="spark" width="120" height="28" viewBox="0 0 120 28" aria-hidden="true">
                  <polyline fill="none" stroke="#f06e6e" stroke-width="1.5" :points="sparkPath('unreadable_pct')" />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback('unreadable_pct') }}</span>
              </div>
              <div class="spark-row">
                <span class="spark-label">In rate / h</span>
                <svg v-if="sparkPath('recv_rate')" class="spark" width="120" height="28" viewBox="0 0 120 28" aria-hidden="true">
                  <polyline fill="none" stroke="#4ea1ff" stroke-width="1.5" :points="sparkPath('recv_rate')" />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback('recv_rate') }}</span>
              </div>
              <div class="spark-row">
                <span class="spark-label">Noise floor</span>
                <svg v-if="sparkPath('noise_floor')" class="spark" width="120" height="28" viewBox="0 0 120 28" aria-hidden="true">
                  <polyline fill="none" stroke="#a78bfa" stroke-width="1.5" :points="sparkPath('noise_floor')" />
                </svg>
                <span v-else class="spark-empty">{{ sparkFallback('noise_floor') }}</span>
              </div>
            </div>
          </section>
          <dl>
            <dt>Unit</dt>
            <dd>{{ selectedUnit.unit_id || selectedUnit.key }}</dd>
            <dt>Site</dt>
            <dd>{{ selectedUnit.site_name || formatSite(selectedUnit.site) }}</dd>
            <dt>Firmware</dt>
            <dd>{{ selectedUnit.firmware_version || '—' }} ({{ selectedUnit.firmware_platform || '?' }})</dd>
            <dt>Bootloader</dt>
            <dd>{{ selectedUnit.bootloader_version || '—' }}</dd>
            <dt>Position</dt>
            <dd>
              <template v-if="selectedUnit.position">
                {{ selectedUnit.position.lat.toFixed(5) }}, {{ selectedUnit.position.lon.toFixed(5) }}
                ({{ selectedUnit.position.source }})
              </template>
              <template v-else>unmapped</template>
            </dd>
            <dt>Voltage</dt>
            <dd>{{ formatBattery(selectedUnit.status?.battery_mv) }}</dd>
            <dt>Temp</dt>
            <dd>{{ formatTemp(selectedUnit.telemetry?.temperature) }}</dd>
            <dt>Uptime</dt>
            <dd>{{ formatUptime(selectedUnit.status?.uptime_secs) }}</dd>
          </dl>
          <section v-if="hasTrafficStats(selectedUnit.status)">
            <h3>Traffic since boot</h3>
            <p class="sub">Packet counters reset when the node reboots.</p>
            <dl>
              <dt>In / out</dt>
              <dd>
                {{ formatCount(selectedUnit.status?.packets_recv) }} /
                {{ formatCount(selectedUnit.status?.packets_sent) }}
              </dd>
              <template v-if="selectedUnit.status?.recv_errors != null">
                <dt>Unreadable RF</dt>
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
                <dt>Recv flood / direct</dt>
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
                <dt>Sent flood / direct</dt>
                <dd>
                  {{ formatCount(selectedUnit.status?.sent_flood) }} /
                  {{ formatCount(selectedUnit.status?.sent_direct) }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.status?.last_snr != null ||
                  selectedUnit.status?.last_rssi != null ||
                  selectedUnit.status?.noise_floor != null
                "
              >
                <dt>Last SNR / RSSI</dt>
                <dd>
                  {{ formatSnr(selectedUnit.status?.last_snr) }} /
                  {{ formatRssi(selectedUnit.status?.last_rssi) }}
                </dd>
                <dt>Noise floor</dt>
                <dd>{{ formatNoiseFloor(selectedUnit.status?.noise_floor) }}</dd>
              </template>
            </dl>
          </section>
          <section v-if="hasTrafficInterval(selectedUnit.traffic_interval)">
            <h3>
              Since last poll ({{
                formatPollWindow(selectedUnit.traffic_interval?.duration_secs)
              }})
            </h3>
            <p v-if="selectedUnit.traffic_interval?.reboot_reset" class="sub">
              Node rebooted during this interval. The next poll will show a clean delta.
            </p>
            <dl v-else>
              <dt>In / out</dt>
              <dd>
                {{ formatCount(selectedUnit.traffic_interval?.packets_recv) }} /
                {{ formatCount(selectedUnit.traffic_interval?.packets_sent) }}
              </dd>
              <template v-if="selectedUnit.traffic_interval?.recv_errors != null">
                <dt>Unreadable RF</dt>
                <dd>
                  {{
                    formatUnreadableRf(
                      selectedUnit.traffic_interval?.recv_errors,
                      selectedUnit.traffic_interval?.packets_recv
                    )
                  }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.traffic_interval?.recv_flood != null ||
                  selectedUnit.traffic_interval?.recv_direct != null
                "
              >
                <dt>Recv flood / direct</dt>
                <dd>
                  {{ formatCount(selectedUnit.traffic_interval?.recv_flood) }} /
                  {{ formatCount(selectedUnit.traffic_interval?.recv_direct) }}
                </dd>
              </template>
              <template
                v-if="
                  selectedUnit.traffic_interval?.sent_flood != null ||
                  selectedUnit.traffic_interval?.sent_direct != null
                "
              >
                <dt>Sent flood / direct</dt>
                <dd>
                  {{ formatCount(selectedUnit.traffic_interval?.sent_flood) }} /
                  {{ formatCount(selectedUnit.traffic_interval?.sent_direct) }}
                </dd>
              </template>
              <template v-if="selectedUnit.traffic_interval?.rx_airtime_pct != null">
                <dt>Channel utilization</dt>
                <dd>{{ formatAirtimePct(selectedUnit.traffic_interval?.rx_airtime_pct) }}</dd>
              </template>
            </dl>
          </section>
          <section v-if="selectedUnit.neighbors?.length">
            <h3>Neighbors ({{ selectedUnit.neighbors.filter(n => n.unit_key).length }} in book)</h3>
            <div
              v-for="(nb, i) in selectedUnit.neighbors.filter(n => n.unit_key)"
              :key="i"
              class="neighbor-row"
            >
              {{ nb.label || nb.unit_id || nb.pubkey_prefix }} · {{ nb.snr ?? '?' }} dB · {{ formatAgo(nb.secs_ago) }}
            </div>
          </section>
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
              <span
                class="health-mark"
                :class="'health-mark-' + healthHeadline(unit)"
                :title="healthTooltip(unit.health)"
              >{{ healthEmoji(unit) }}</span>
              <span class="unit-title">{{ cardTitle(unit) }}</span>
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
            <div class="unit-row2">{{ cardMeta(unit) }}</div>
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
