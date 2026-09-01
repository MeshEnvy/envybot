import { createApp, computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { connectEvents, fetchFleet, patchUnit } from './api.js'
import {
  formatBattery,
  formatCount,
  formatErrorRate,
  formatNoiseFloor,
  formatRelative,
  formatRssi,
  formatSite,
  formatSnr,
  formatTemp,
  formatUptime,
  hasTrafficStats,
  unitLabel,
  unitTitle,
} from './format.js?v=4'
import { createMapController, unitStatus } from './map.js?v=11'

const SESSION_RANK = {
  polling: 0,
  unreachable: 1,
  queued: 2,
  ok: 3,
  fresh: 4,
  stale: 5,
  never: 6,
  unmapped: 7,
}

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

    const sortedUnits = computed(() => {
      const q = search.value.trim().toLowerCase()
      let units = Object.values(fleet.units || {})
      if (q) {
        units = units.filter((u) => {
          const hay = [u.key, u.unit_id, u.name, u.site, u.site_name, u.label]
            .filter(Boolean)
            .join(' ')
            .toLowerCase()
          return hay.includes(q)
        })
      }
      return units.sort((a, b) => {
        const ra = SESSION_RANK[unitStatus(a)] ?? 8
        const rb = SESSION_RANK[unitStatus(b)] ?? 8
        if (ra !== rb) return ra - rb
        const ha = a.last_heard ?? 0
        const hb = b.last_heard ?? 0
        if (ha !== hb) return hb - ha
        return unitLabel(a).localeCompare(unitLabel(b))
      })
    })

    const selectedUnit = computed(() => (selectedKey.value ? fleet.units[selectedKey.value] : null))

    function syncDetailPopup() {
      const unit = selectedUnit.value
      const el = detailEl.value
      if (!unit?.position || !el) {
        mapCtrl?.detachDetail()
        return
      }
      const lat = Number(unit.position.lat)
      const lon = Number(unit.position.lon)
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        mapCtrl?.detachDetail()
        return
      }
      mapCtrl.attachDetail(el, [lon, lat])
    }

    function clearSelection() {
      selectedKey.value = null
      mapCtrl?.detachDetail()
      mapCtrl?.sync(fleet, null)
    }

    async function selectUnit(key) {
      if (selectedKey.value === key) {
        clearSelection()
        return
      }
      selectedKey.value = key
      mapCtrl?.flyTo(key, fleet)
      mapCtrl?.sync(fleet, key)
      await nextTick()
      syncDetailPopup()
    }

    function cardMeta(unit) {
      const parts = unit.site_name ? [unit.unit_id || unit.key] : [formatSite(unit.site)]
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
      sortedUnits,
      selectedUnit,
      selectedKey,
      detailEl,
      pollLine,
      selectUnit,
      clearSelection,
      unitStatus,
      cardMeta,
      formatRelative,
      formatSite,
      formatBattery,
      formatCount,
      formatErrorRate,
      formatNoiseFloor,
      formatTemp,
      formatUptime,
      formatRssi,
      formatSnr,
      hasTrafficStats,
      unitLabel,
      unitTitle,
      togglePublic,
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
      </div>
      <div class="search-wrap">
        <input v-model="search" type="search" placeholder="Search site, unit, name…" autocomplete="off" />
      </div>
    </header>
    <div id="layout">
      <main id="map-wrap">
        <div id="map"></div>
        <div
          v-if="selectedUnit && selectedUnit.position"
          ref="detailEl"
          id="detail"
          class="detail"
        >
          <div class="detail-head">
            <h2>{{ unitTitle(selectedUnit) }}</h2>
            <button type="button" class="detail-close" aria-label="Close" @click="clearSelection">×</button>
          </div>
          <p class="sub">
            {{ selectedUnit.unit_id }}
            · {{ selectedUnit.public ? 'public' : 'private' }}
            · {{ unitStatus(selectedUnit) }}
            · {{ formatRelative(selectedUnit.last_heard) }}
            <span v-if="selectedUnit.drift"> · {{ selectedUnit.drift }}</span>
          </p>
          <label class="public-toggle">
            <input type="checkbox" :checked="!!selectedUnit.public" @change="togglePublic(selectedUnit, $event)" />
            public (push book name + GPS)
          </label>
          <dl>
            <dt>Book name</dt>
            <dd>{{ selectedUnit.name || '—' }}</dd>
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
            <h3>Traffic</h3>
            <dl>
              <dt>Recv / sent</dt>
              <dd>
                {{ formatCount(selectedUnit.status?.packets_recv) }} /
                {{ formatCount(selectedUnit.status?.packets_sent) }}
              </dd>
              <dt>RX errors</dt>
              <dd>
                {{ formatCount(selectedUnit.status?.recv_errors) }}
                ({{ formatErrorRate(selectedUnit.status?.recv_errors, selectedUnit.status?.packets_recv) }})
              </dd>
              <dt>Queue full</dt>
              <dd>{{ formatCount(selectedUnit.status?.err_events) }}</dd>
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
          <section v-if="selectedUnit.neighbors?.length">
            <h3>Neighbors ({{ selectedUnit.neighbors.filter(n => n.unit_key).length }} in book)</h3>
            <div
              v-for="(nb, i) in selectedUnit.neighbors.filter(n => n.unit_key)"
              :key="i"
              class="neighbor-row"
            >
              {{ nb.label || nb.unit_id || nb.pubkey_prefix }} · {{ nb.snr ?? '?' }} dB · {{ nb.secs_ago ?? '?' }}s
            </div>
          </section>
        </div>
      </main>
      <aside id="sidebar">
        <div id="unit-list">
          <div
            v-for="unit in sortedUnits"
            :key="unit.key"
            class="unit-card"
            :class="{ selected: unit.key === selectedKey }"
            @click="selectUnit(unit.key)"
          >
            <div class="unit-top">
              <span v-if="!unit.site_name" class="unit-id">{{ unit.unit_id || unit.key }}</span>
              <span class="unit-name">{{ unit.site_name || unit.name || unitLabel(unit) }}</span>
              <span class="unit-badges">
                <span class="badge" :class="'badge-' + unitStatus(unit)">{{ unitStatus(unit) }}</span>
                <span v-if="unit.drift" class="badge" :class="'badge-' + unit.drift">{{ unit.drift }}</span>
              </span>
            </div>
            <div class="unit-meta">{{ cardMeta(unit) }}</div>
          </div>
        </div>
      </aside>
    </div>
  `,
}

createApp(App).mount('#app')
