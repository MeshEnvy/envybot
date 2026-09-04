import maplibregl from 'maplibre-gl'

const PIN_STALE_SECS = 86400
const PIN_NEVER = '#8aa0b5'
const PIN_FRESH = [0x6e, 0xe7, 0xa0]
const PIN_MID = [0xf0, 0xb8, 0x6e]
const PIN_STALE = [0xf0, 0x6e, 0x6e]

function hexRgb(rgb) {
  return `#${rgb.map((c) => Math.round(c).toString(16).padStart(2, '0')).join('')}`
}

function lerpRgb(a, b, t) {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
}

/** Green at 0s → amber at 12h → red at 24h+. Never-heard is gray. */
export function pinFreshnessColor(lastHeard, now = Date.now() / 1000) {
  if (lastHeard == null || !Number.isFinite(Number(lastHeard))) return PIN_NEVER
  const t = Math.min(1, Math.max(0, now - Number(lastHeard)) / PIN_STALE_SECS)
  if (t <= 0.5) return hexRgb(lerpRgb(PIN_FRESH, PIN_MID, t / 0.5))
  return hexRgb(lerpRgb(PIN_MID, PIN_STALE, (t - 0.5) / 0.5))
}

/** Largest unit only: 12s, 5m, 3h, 1d. */
export function formatMapAge(secs) {
  const s = Math.max(0, Math.floor(Number(secs)))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

/** @param {Record<string, unknown> | undefined} unit */
export function mapPinLabel(unit, now = Date.now() / 1000) {
  const name = String(unit?.label || unit?.site_name || unit?.alias || unit?.unit_id || unit?.key || '')
  const heard = unit?.last_heard
  if (heard == null || !Number.isFinite(Number(heard))) return name
  const age = Math.max(0, Math.floor(now - Number(heard)))
  return `${name} (${formatMapAge(age)})`
}

/** @param {Record<string, Record<string, unknown>> | undefined} units */
export function buildNeighborEdges(units) {
  /** @type {Array<{ from: string, to: string, coordinates: number[][] }>} */
  const edges = []
  const seen = new Set()
  for (const [key, unit] of Object.entries(units || {})) {
    if (!hasMapPin(unit.position)) continue
    const pos = /** @type {{ lon: number, lat: number }} */ (unit.position)
    for (const nb of /** @type {Array<{ unit_key?: string }>} */ (unit.neighbors || [])) {
      const peerKey = nb.unit_key
      if (!peerKey || !units[peerKey]) continue
      const peer = units[peerKey]
      if (!hasMapPin(peer.position)) continue
      const peerPos = /** @type {{ lon: number, lat: number }} */ (peer.position)
      const pair = [key, peerKey].sort().join('\0')
      if (seen.has(pair)) continue
      seen.add(pair)
      edges.push({
        from: key,
        to: peerKey,
        coordinates: [
          [pos.lon, pos.lat],
          [peerPos.lon, peerPos.lat],
        ],
      })
    }
  }
  return edges
}

/** @param {unknown} pos */
export function hasMapPin(pos) {
  if (!pos || typeof pos !== 'object') return false
  const lat = Number(/** @type {{ lat?: unknown }} */ (pos).lat)
  const lon = Number(/** @type {{ lon?: unknown }} */ (pos).lon)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return false
  if (Math.abs(lat) < 0.001 || Math.abs(lon) < 0.001) return false
  return true
}

/** @param {Record<string, unknown> | undefined} unit */
export function unitStatus(unit) {
  const s = unit?.session
  const state = s && typeof s === 'object' && 'state' in s ? s.state : null
  if (state === 'polling') return 'polling'
  if (state === 'queued') return 'queued'
  if (state === 'refreshing') return 'refreshing'
  if (state === 'pulling') return 'pulling'
  if (state === 'pushing') return 'pushing'
  const headline =
    unit?.health && typeof unit.health === 'object' && 'headline' in unit.health
      ? unit.health.headline
      : null
  if (headline === 'paused' || headline === 'unreachable' || headline === 'attention' || headline === 'healthy') {
    return headline
  }
  if (unit?.paused) return 'paused'
  if (state === 'unreachable') return 'unreachable'
  if (!unit?.mapped) return 'unmapped'
  return typeof unit?.freshness === 'string' ? unit.freshness : 'never'
}

/** Badge text: current job stage when in flight, else unitStatus. */
export function unitStage(unit) {
  const s = unit?.session
  if (s && typeof s === 'object' && s.state === 'console') return 'Console'
  const stage = s && typeof s === 'object' && typeof s.stage === 'string' ? s.stage.trim() : ''
  if (stage) return stage
  return unitStatus(unit)
}

/**
 * @param {string} containerId
 * @param {(key: string) => void} onSelect
 * @param {() => void} [onClear]
 */
export function createMapController(containerId, onSelect, onClear) {
  const container = document.getElementById(containerId)
  if (!container) {
    throw new Error(`map container #${containerId} not found`)
  }

  const map = new maplibregl.Map({
    container,
    style: {
      version: 8,
      glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
      sources: {
        osm: {
          type: 'raster',
          tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
          attribution: '© OpenStreetMap',
        },
      },
      layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
    },
    center: [-119.5, 39.2],
    zoom: 6,
  })

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right')

  let ready = false
  let fitted = false
  /** @type {Record<string, unknown> | null} */
  let pendingFleet = null
  /** @type {string | null} */
  let pendingSelected = null
  /** @type {number | undefined} */
  let pendingNow = undefined

  const colorExpr = ['get', 'pin']

  function ensureLayers() {
    if (map.getSource('units')) return

    map.addSource('edges', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    })
    map.addLayer({
      id: 'edges',
      type: 'line',
      source: 'edges',
      paint: {
        'line-color': '#4ea1ff',
        'line-width': 1.5,
        'line-opacity': 0.45,
      },
    })

    map.addSource('units', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    })
    map.addLayer({
      id: 'units-glow',
      type: 'circle',
      source: 'units',
      paint: {
        'circle-radius': ['case', ['==', ['get', 'selected'], 1], 18, 14],
        'circle-color': colorExpr,
        'circle-opacity': 0.35,
        'circle-blur': 0.4,
      },
    })
    map.addLayer({
      id: 'units',
      type: 'circle',
      source: 'units',
      paint: {
        'circle-radius': ['case', ['==', ['get', 'selected'], 1], 11, 9],
        'circle-color': colorExpr,
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
        'circle-opacity': 1,
      },
    })
    map.addLayer({
      id: 'units-label',
      type: 'symbol',
      source: 'units',
      layout: {
        'text-field': ['get', 'label'],
        'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
        'text-size': 11,
        'text-offset': [0, 1.3],
        'text-anchor': 'top',
      },
      paint: {
        'text-color': '#e7ecf1',
        'text-halo-color': '#0f1419',
        'text-halo-width': 1.5,
      },
    })

    const hitLayers = () =>
      ['units', 'units-glow', 'units-label'].filter((id) => map.getLayer(id))

    map.on('click', (e) => {
      const hits = map.queryRenderedFeatures(e.point, { layers: hitLayers() })
      const key = hits[0]?.properties?.key
      if (typeof key === 'string') onSelect(key)
      else onClear?.()
    })
    for (const layer of ['units', 'units-glow', 'units-label']) {
      map.on('mouseenter', layer, () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', layer, () => {
        map.getCanvas().style.cursor = ''
      })
    }
  }

  /** @param {Record<string, unknown>} fleet @param {string | null} selectedKey @param {number} [now] */
  function sync(fleet, selectedKey, now = Date.now() / 1000) {
    if (!ready) {
      pendingFleet = fleet
      pendingSelected = selectedKey
      pendingNow = now
      return
    }
    ensureLayers()

    const units = /** @type {Record<string, Record<string, unknown>>} */ (fleet.units || {})
    /** @type {GeoJSON.Feature[]} */
    const features = []
    for (const unit of Object.values(units)) {
      const pos = unit.position
      if (!hasMapPin(pos)) continue
      const lat = Number(/** @type {{ lat?: unknown }} */ (pos).lat)
      const lon = Number(/** @type {{ lon?: unknown }} */ (pos).lon)
      features.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [lon, lat] },
        properties: {
          key: String(unit.key),
          label: mapPinLabel(unit, now),
          pin: pinFreshnessColor(unit.last_heard, now),
          selected: unit.key === selectedKey ? 1 : 0,
        },
      })
    }

    const src = /** @type {import('maplibre-gl').GeoJSONSource} */ (map.getSource('units'))
    src.setData({ type: 'FeatureCollection', features })

    const edges = /** @type {Array<{ coordinates: number[][] }>} */ (fleet.edges || [])
    const edgeSrc = /** @type {import('maplibre-gl').GeoJSONSource} */ (map.getSource('edges'))
    edgeSrc.setData({
      type: 'FeatureCollection',
      features: edges
        .filter((e) => Array.isArray(e.coordinates) && e.coordinates.length >= 2)
        .map((e) => ({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: e.coordinates },
          properties: {},
        })),
    })

    if (!fitted && features.length) {
      const bounds = new maplibregl.LngLatBounds()
      for (const f of features) {
        if (f.geometry?.type === 'Point') {
          bounds.extend(/** @type {[number, number]} */ (f.geometry.coordinates))
        }
      }
      if (!bounds.isEmpty()) {
        map.fitBounds(bounds, { padding: 60, maxZoom: 10 })
        fitted = true
      }
    }
  }

  function onReady() {
    if (ready) return
    ready = true
    ensureLayers()
    map.resize()
    if (pendingFleet) {
      sync(pendingFleet, pendingSelected, pendingNow)
      pendingFleet = null
    }
  }

  if (map.loaded()) onReady()
  else map.once('load', onReady)

  map.on('error', (e) => {
    console.error('map error', e.error)
  })

  const ro = new ResizeObserver(() => {
    map.resize()
  })
  ro.observe(container)
  const wrap = container.parentElement
  if (wrap) ro.observe(wrap)

  /** @param {string | null} key @param {Record<string, unknown>} fleet */
  function flyTo(key, fleet) {
    const unit = key ? /** @type {Record<string, unknown>} */ (fleet.units)?.[key] : null
    const pos = unit?.position
    if (!hasMapPin(pos)) return
    const lat = Number(/** @type {{ lat?: unknown }} */ (pos).lat)
    const lon = Number(/** @type {{ lon?: unknown }} */ (pos).lon)
    map.flyTo({
      center: [lon, lat],
      zoom: 11,
      speed: 1.2,
    })
  }

  /** @param {number} clientX @param {number} clientY */
  function hitUnit(clientX, clientY) {
    if (!ready || !map.getLayer('units')) return null
    const rect = map.getCanvas().getBoundingClientRect()
    const point = [clientX - rect.left, clientY - rect.top]
    const layers = ['units', 'units-glow', 'units-label'].filter((id) =>
      map.getLayer(id),
    )
    const hits = map.queryRenderedFeatures(point, { layers })
    const key = hits[0]?.properties?.key
    return typeof key === 'string' ? key : null
  }

  return {
    sync,
    flyTo,
    hitUnit,
    destroy() {
      ro.disconnect()
      map.remove()
    },
  }
}
