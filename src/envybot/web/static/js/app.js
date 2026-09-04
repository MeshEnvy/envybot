import {
  createApp,
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from "vue";
import {
  connectEvents,
  fetchFleet,
  fetchPolls,
  installUnit,
  patchUnit,
  postBenchLoc,
  pullUnit,
  pushUnit,
  refreshUnit,
  stageUnit,
  openConsole as apiOpenConsole,
  sendConsole as apiSendConsole,
  cancelConsole as apiCancelConsole,
  closeConsole as apiCloseConsole,
  parkConsole as apiParkConsole,
  retryConsole as apiRetryConsole,
  skipConsole as apiSkipConsole,
  patchConsolePending as apiPatchPending,
  deleteConsolePending as apiDeletePending,
  clearConsoleHistory as apiClearHistory,
} from "./api.js";
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
} from "./state.js?v=8";
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
  healthEmoji,
  showHealthMark,
  healthTooltip,
  compareUnits,
  unitLabel,
  unitTitle,
} from "./format.js?v=26";
import {
  buildNeighborEdges,
  createMapController,
  unitStage,
  unitStatus,
} from "./map.js?v=30";
import {
  seriesFromHistories,
  sparklineWallTime,
  SPARK_MIN_SPAN,
} from "./sparklines.js?v=10";

const DASH_SPARK_W = 52;
const DASH_SPARK_H = 16;

const BENCH_MOVE_M = 200;
const BENCH_MAX_SEC = 60;

/** @param {number} lat1 @param {number} lon1 @param {number} lat2 @param {number} lon2 */
function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

/** Debounced browser GPS → nodes.yaml bench_loc (map pins only). */
function startBenchGeolocation() {
  if (!navigator.geolocation) return () => {};
  /** @type {{ lat: number, lon: number, ts: number } | null} */
  let lastSent = null;
  const watchId = navigator.geolocation.watchPosition(
    (pos) => {
      const lat = pos.coords.latitude;
      const lon = pos.coords.longitude;
      const now = Date.now();
      if (
        lastSent &&
        haversineM(lastSent.lat, lastSent.lon, lat, lon) < BENCH_MOVE_M &&
        now - lastSent.ts < BENCH_MAX_SEC * 1000
      ) {
        return;
      }
      lastSent = { lat, lon, ts: now };
      postBenchLoc(lat, lon).catch((err) => console.warn("bench_loc", err));
    },
    () => {},
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 },
  );
  return () => navigator.geolocation.clearWatch(watchId);
}

const App = {
  setup() {
    const fleet = fleetStore;
    const selectedKey = ref(null);
    const mapOpen = ref(false);
    const mapHighlightKey = ref(/** @type {string | null} */ (null));
    const search = ref("");
    /** @type {import('vue').Ref<HTMLInputElement | null>} */
    const searchInput = ref(null);
    const listFilter = ref("all");
    const aliasDraft = ref("");
    const notesDraft = ref("");
    const aliasEditing = ref(false);
    const notesEditing = ref(false);
    const historyHours = 72;
    const LOG_PAGE = 10;
    const logShown = reactive({
      status: LOG_PAGE,
      telemetry: LOG_PAGE,
      neighbors: LOG_PAGE,
      acl: LOG_PAGE,
    });
    const CONSOLE_STORE_KEY = "envybot.console.v1";
    const CMD_HISTORY_CAP = 100;
    const consoleOpen = ref(false);
    const consoleActiveId = ref(/** @type {string | null} */ (null));
    const consolePickerOpen = ref(false);
    const consolePickerQuery = ref("");
    /** @type {import('vue').Ref<HTMLElement | null>} */
    const consoleScroll = ref(null);
    /** @type {import('vue').Ref<HTMLInputElement | null>} */
    const consolePickerSearch = ref(null);
    /** @typedef {{ tab_id: string, key: string, state: string, attempt?: number, max_attempts?: number, cmd?: string | null, error?: string | null, history: { cmd: string, reply?: string, error?: string }[], pending: { id: string, cmd: string }[], draft: string, unread?: boolean, cmds?: string[], cmdIndex?: number | null, cmdHold?: string }} ConsoleTab */
    const consoleTabs = reactive(/** @type {ConsoleTab[]} */ ([]));
    let consoleSeeding = false;
    /** localStorage seed runs once per page load, not on every SSE hello */
    let consoleBootstrapped = false;
    const consoleCopied = ref(false);
    let consoleCopyTimer = 0;

    function resetLogShown() {
      logShown.status = LOG_PAGE;
      logShown.telemetry = LOG_PAGE;
      logShown.neighbors = LOG_PAGE;
      logShown.acl = LOG_PAGE;
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function logSlice(source) {
      const list = unitHistory.value?.[source];
      if (!Array.isArray(list)) return [];
      return list.slice(0, logShown[source]);
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function logHasMore(source) {
      const list = unitHistory.value?.[source];
      return Array.isArray(list) && list.length > logShown[source];
    }

    /** @param {'status' | 'telemetry' | 'neighbors' | 'acl'} source */
    function loadMoreLog(source) {
      logShown[source] += LOG_PAGE;
    }

    const METRIC_ROWS = [
      { key: "sun", label: "Sun", stroke: "#f5c14a", wave: true },
      { key: "battery_mv", label: "Voltage", stroke: "#6ee7a0" },
      { key: "temperature", label: "Temp", stroke: "#f0b86e" },
      { key: "unreadable_pct", label: "Unreadable", stroke: "#f06e6e" },
      { key: "recv_rate", label: "In / h", stroke: "#4ea1ff" },
      { key: "noise_floor", label: "Noise", stroke: "#a78bfa" },
    ];
    /** @type {ReturnType<typeof createMapController> | null} */
    let mapCtrl = null;
    /** @type {EventSource | null} */
    let es = null;
    let stopBenchGeo = null;

    function applyHello(snap) {
      replaceSnapshot(snap);
      return reconcileConsole(snap);
    }

    function applyUnit(unit) {
      storePatchUnit(unit);
    }

    function applySession(poll) {
      patchSession(poll);
    }

    const manualAccepting = computed(() => !!fleet.poll?.accepting);

    const consoleActive = computed(
      () => consoleTabs.find((t) => t.tab_id === consoleActiveId.value) || null,
    );
    const consoleBusy = computed(
      () => consoleActive.value?.state === "sending",
    );
    const consoleFailed = computed(
      () => consoleActive.value?.state === "failed",
    );
    const consoleStatusLine = computed(() => {
      const tab = consoleActive.value;
      if (!tab || tab.state !== "sending") return "";
      const n = tab.attempt || 1;
      const max = tab.max_attempts || 10;
      return `(attempt ${n}/${max}…)`;
    });
    const consoleBadgeBusy = computed(() =>
      consoleTabs.some(
        (t) =>
          t.state === "sending" ||
          t.state === "failed" ||
          (t.pending && t.pending.length),
      ),
    );
    const consoleUnread = computed(
      () => !consoleOpen.value && consoleTabs.some((t) => t.unread),
    );
    const consolePickerUnits = computed(() => {
      const q = consolePickerQuery.value.trim().toLowerCase();
      let units = Object.values(fleet.units || {});
      if (q) {
        units = units.filter((u) => {
          const hay = [
            u.key,
            u.unit_id,
            u.site,
            u.site_name,
            u.alias,
            u.label,
            u.notes,
          ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
          return hay.includes(q);
        });
      }
      return units.sort(compareUnits);
    });

    const MANUAL_BUSY = new Set([
      "queued",
      "refreshing",
      "pulling",
      "pushing",
      "staging",
      "installing",
      "polling",
    ]);

    /** @param {Record<string, unknown> | undefined} unit */
    function isInFlight(unit) {
      const s = unit?.session;
      const state = s && typeof s === "object" && "state" in s ? s.state : null;
      return MANUAL_BUSY.has(state);
    }

    /** @param {Record<string, unknown> | undefined} unit @param {'refresh' | 'pull' | 'push'} [job] */
    function canManualUnit(unit, job) {
      if (!unit || !manualAccepting.value) return false;
      return true;
    }

    function newConsoleTabId() {
      if (typeof crypto !== "undefined" && crypto.randomUUID)
        return crypto.randomUUID();
      return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    }

    function loadConsoleStore() {
      try {
        const raw = localStorage.getItem(CONSOLE_STORE_KEY);
        if (!raw) return { tabs: [], active: null };
        const parsed = JSON.parse(raw);
        if (!parsed || !Array.isArray(parsed.tabs))
          return { tabs: [], active: null };
        return parsed;
      } catch {
        return { tabs: [], active: null };
      }
    }

    function historySig(history) {
      if (!Array.isArray(history) || !history.length) return "0";
      const last = history[history.length - 1];
      return `${history.length}:${last.cmd || ""}:${last.reply || ""}:${last.error || ""}`;
    }

    function consoleTabWatched(tabId) {
      return consoleOpen.value && consoleActiveId.value === tabId;
    }

    function seeConsoleTab(tabId) {
      const tab = consoleTabs.find((t) => t.tab_id === tabId);
      if (!tab?.unread) return;
      tab.unread = false;
      persistConsole();
    }

    function selectConsoleTab(tabId) {
      consoleActiveId.value = tabId;
      seeConsoleTab(tabId);
    }

    function unreadFromStore(tabId) {
      return !!(loadConsoleStore().tabs || []).find((t) => t.tab_id === tabId)
        ?.unread;
    }

    function cmdsFromHistory(history) {
      const out = [];
      for (const row of history || []) {
        const cmd = String(row?.cmd || "").trim();
        if (!cmd) continue;
        if (out[out.length - 1] !== cmd) out.push(cmd);
      }
      return out.slice(-CMD_HISTORY_CAP);
    }

    function loadCmds(tabId, history) {
      const stored = (loadConsoleStore().tabs || []).find(
        (t) => t.tab_id === tabId,
      );
      if (stored && Array.isArray(stored.cmds))
        return stored.cmds.slice(-CMD_HISTORY_CAP);
      return cmdsFromHistory(history);
    }

    function rememberCmd(tab, cmd) {
      const line = String(cmd || "").trim();
      if (!tab || !line) return;
      const list = Array.isArray(tab.cmds) ? tab.cmds : (tab.cmds = []);
      if (list[list.length - 1] !== line) list.push(line);
      if (list.length > CMD_HISTORY_CAP)
        tab.cmds = list.slice(-CMD_HISTORY_CAP);
      tab.cmdIndex = null;
      tab.cmdHold = "";
    }

    function recallConsoleCmd(dir) {
      const tab = consoleActive.value;
      const list = tab?.cmds || [];
      if (!tab || !list.length) return;
      if (tab.cmdIndex == null) {
        tab.cmdHold = tab.draft || "";
        tab.cmdIndex = list.length;
      }
      const next = tab.cmdIndex + dir;
      if (next < 0) return;
      if (next >= list.length) {
        tab.cmdIndex = null;
        tab.draft = tab.cmdHold || "";
        return;
      }
      tab.cmdIndex = next;
      tab.draft = list[next];
    }

    function onConsoleDraftInput() {
      const tab = consoleActive.value;
      if (!tab || tab.cmdIndex == null) return;
      tab.cmdIndex = null;
      tab.cmdHold = "";
    }

    function persistConsole() {
      const tabs = consoleTabs.map((t) => ({
        tab_id: t.tab_id,
        key: t.key,
        history: t.history || [],
        pending: t.pending || [],
        draft: t.draft || "",
        inflight: t.state === "sending" && t.cmd ? t.cmd : undefined,
        failed: t.state === "failed" && t.cmd ? t.cmd : undefined,
        error: t.state === "failed" ? t.error || "" : undefined,
        unread: !!t.unread,
        cmds: Array.isArray(t.cmds) ? t.cmds.slice(-CMD_HISTORY_CAP) : [],
      }));
      try {
        localStorage.setItem(
          CONSOLE_STORE_KEY,
          JSON.stringify({ tabs, active: consoleActiveId.value }),
        );
      } catch {
        /* ignore quota */
      }
    }

    /** @param {Record<string, unknown>} event @param {string} [draft] */
    function upsertConsoleTab(event, draft) {
      const tabId = String(event.tab_id || "");
      if (!tabId || event.state === "closed") {
        const idx = consoleTabs.findIndex((t) => t.tab_id === tabId);
        if (idx >= 0) consoleTabs.splice(idx, 1);
        if (consoleActiveId.value === tabId) {
          consoleActiveId.value = consoleTabs[0]?.tab_id || null;
        }
        persistConsole();
        return;
      }
      const existing = consoleTabs.find((t) => t.tab_id === tabId);
      const keepDraft = draft ?? existing?.draft ?? "";
      const nextHistory = Array.isArray(event.history)
        ? event.history
        : existing?.history || [];
      const watched = consoleTabWatched(tabId);
      let unread = existing ? !!existing.unread : unreadFromStore(tabId);
      if (
        existing &&
        !consoleSeeding &&
        !watched &&
        (historySig(existing.history) !== historySig(nextHistory) ||
          (String(event.state || "") === "failed" &&
            existing.state !== "failed"))
      ) {
        unread = true;
      }
      if (watched) unread = false;
      const cmds = Array.isArray(existing?.cmds)
        ? existing.cmds
        : loadCmds(tabId, nextHistory);
      const next = {
        tab_id: tabId,
        key: String(event.key || existing?.key || ""),
        state: String(event.state || "ready"),
        attempt: Number(event.attempt || 0),
        max_attempts: Number(event.max_attempts || 10),
        cmd: event.cmd == null ? null : String(event.cmd),
        error: event.error == null ? null : String(event.error),
        history: nextHistory,
        pending: Array.isArray(event.pending)
          ? event.pending
          : existing?.pending || [],
        draft: keepDraft,
        unread,
        cmds,
        cmdIndex: existing?.cmdIndex ?? null,
        cmdHold: existing?.cmdHold ?? "",
      };
      if (existing) Object.assign(existing, next);
      else consoleTabs.push(next);
      persistConsole();
    }

    function scrollConsoleBottom() {
      requestAnimationFrame(() => {
        const el = consoleScroll.value;
        if (el) el.scrollTop = el.scrollHeight;
      });
    }

    /** @param {Record<string, unknown>} event */
    function applyConsoleEvent(event) {
      patchConsole(event);
      upsertConsoleTab(event);
      if (event.tab_id === consoleActiveId.value) scrollConsoleBottom();
    }

    function consoleTabLabel(tab) {
      const unit = fleet.units[tab.key];
      const base = unit ? unitTitle(unit) : tab.key;
      const same = consoleTabs.filter((t) => t.key === tab.key);
      if (same.length < 2) return base;
      const n = same.findIndex((t) => t.tab_id === tab.tab_id) + 1;
      return n === 1 ? base : `${base} ${n}`;
    }

    async function addConsoleTab(key) {
      const tabId = newConsoleTabId();
      const tab = {
        tab_id: tabId,
        key: String(key).toLowerCase(),
        state: "ready",
        history: [],
        pending: [],
        draft: "",
        unread: false,
        cmds: [],
        cmdIndex: null,
        cmdHold: "",
      };
      consoleTabs.push(tab);
      consoleActiveId.value = tabId;
      consolePickerOpen.value = false;
      consolePickerQuery.value = "";
      persistConsole();
      try {
        const ev = await apiOpenConsole(tab.key, tabId);
        upsertConsoleTab(ev, tab.draft);
        patchConsole(ev);
      } catch (err) {
        console.error(err);
      }
      syncLocation(selectedKey.value);
    }

    async function closeConsoleTab(tabId) {
      try {
        await apiCloseConsole(tabId);
      } catch (err) {
        console.error(err);
      }
      upsertConsoleTab({ tab_id: tabId, state: "closed" });
      persistConsole();
      syncLocation(selectedKey.value);
    }

    function hideConsole() {
      consoleOpen.value = false;
      consolePickerOpen.value = false;
      syncLocation(selectedKey.value);
    }

    function hideMap() {
      mapOpen.value = false;
      syncLocation(selectedKey.value);
    }

    async function initMapIfNeeded() {
      if (mapCtrl) {
        pushMap();
        return;
      }
      await new Promise((r) =>
        requestAnimationFrame(() => requestAnimationFrame(r)),
      );
      mapCtrl = createMapController("map", selectFromMapPin, () => {
        if (selectedKey.value) clearSelection();
      });
      pushMap();
      requestAnimationFrame(() => pushMap());
    }

    async function showMap() {
      hideConsole();
      mapOpen.value = true;
      syncLocation(selectedKey.value);
      await nextTick();
      await initMapIfNeeded();
    }

    function toggleMap() {
      if (mapOpen.value) hideMap();
      else showMap();
    }

    function mapFromLocation() {
      return new URLSearchParams(location.search).get("map") === "1";
    }

    async function maybeReopenMap() {
      if (!mapFromLocation()) return;
      await showMap();
    }

    function toggleConsolePicker() {
      consolePickerOpen.value = !consolePickerOpen.value;
      if (!consolePickerOpen.value) {
        consolePickerQuery.value = "";
        return;
      }
      nextTick(() => consolePickerSearch.value?.focus());
    }

    async function showConsole() {
      hideMap();
      consoleOpen.value = true;
      if (!consoleTabs.length && selectedKey.value) {
        await addConsoleTab(selectedKey.value);
      } else if (!consoleActiveId.value && consoleTabs[0]) {
        consoleActiveId.value = consoleTabs[0].tab_id;
      }
      if (consoleActiveId.value) seeConsoleTab(consoleActiveId.value);
      syncLocation(selectedKey.value);
      scrollConsoleBottom();
    }

    async function toggleConsole() {
      if (consoleOpen.value) hideConsole();
      else await showConsole();
    }

    function consoleTabsForUnit(key) {
      const k = String(key || "").toLowerCase();
      return consoleTabs.filter((t) => t.key === k);
    }

    function unitConsoleUnread(unit) {
      return consoleTabsForUnit(unit?.key).some((t) => t.unread);
    }

    function unitConsoleBusy(unit) {
      return consoleTabsForUnit(unit?.key).some(
        (t) =>
          t.state === "sending" ||
          t.state === "failed" ||
          (t.pending && t.pending.length),
      );
    }

    async function openConsoleForUnit(unit, ev) {
      ev?.stopPropagation?.();
      const key = String(unit?.key || "").toLowerCase();
      if (!key) return;
      if (consoleOpen.value && consoleActive.value?.key === key) {
        hideConsole();
        return;
      }
      const existing = consoleTabs.find((t) => t.key === key);
      if (existing) selectConsoleTab(existing.tab_id);
      else await addConsoleTab(key);
      await showConsole();
    }

    function failConsoleLocal(tab, cmd, err) {
      const msg = err?.message || String(err || "send failed");
      tab.state = "failed";
      tab.cmd = cmd;
      tab.error = msg;
      tab.history = [...(tab.history || []), { cmd, error: msg }];
    }

    async function submitConsoleLine() {
      const tab = consoleActive.value;
      const cmd = (tab?.draft || "").trim();
      if (!tab || !cmd) return;
      rememberCmd(tab, cmd);
      tab.draft = "";
      persistConsole();
      try {
        await apiSendConsole(tab.tab_id, cmd);
      } catch (err) {
        failConsoleLocal(tab, cmd, err);
      }
    }

    async function cancelConsoleSend() {
      const tab = consoleActive.value;
      if (!tab) return;
      try {
        await apiCancelConsole(tab.tab_id);
      } catch (err) {
        console.error(err);
      }
    }

    function consoleIsStopped(error) {
      const text = String(error || "").toLowerCase();
      return (
        text.includes("timeout") ||
        text.includes("cancelled") ||
        text.includes("canceled")
      );
    }

    function consoleHistoryRetry(row) {
      if (!consoleIsStopped(row?.error)) return false;
      const tab = consoleActive.value;
      if (!tab) return false;
      if (tab.state === "sending") return false;
      if (tab.state === "failed" && tab.cmd === row.cmd) return false;
      return true;
    }

    async function retryConsoleSend() {
      const tab = consoleActive.value;
      if (!tab) return;
      try {
        await apiRetryConsole(tab.tab_id);
      } catch (err) {
        failConsoleLocal(tab, tab.cmd || "", err);
      }
    }

    async function retryConsoleCmd(cmd) {
      const tab = consoleActive.value;
      const line = String(cmd || "").trim();
      if (!tab || !line) return;
      rememberCmd(tab, line);
      try {
        if (tab.state === "failed" && tab.cmd === line)
          await apiRetryConsole(tab.tab_id);
        else await apiSendConsole(tab.tab_id, line);
      } catch (err) {
        failConsoleLocal(tab, line, err);
      }
    }

    async function skipConsoleFailed() {
      const tab = consoleActive.value;
      if (!tab || !tab.pending?.length) return;
      try {
        await apiSkipConsole(tab.tab_id);
      } catch (err) {
        console.error(err);
      }
    }

    async function clearConsoleHistory() {
      const tab = consoleActive.value;
      if (!tab) return;
      try {
        const ev = await apiClearHistory(tab.tab_id);
        upsertConsoleTab(ev, tab.draft);
      } catch (err) {
        console.error(err);
      }
    }

    function consoleTranscriptText(tab) {
      if (!tab) return "";
      const blocks = [];
      for (const row of tab.history || []) {
        const parts = [`> ${row.cmd}`];
        if (row.reply) parts.push(String(row.reply).replace(/\s+$/g, ""));
        if (row.error) parts.push(String(row.error).replace(/\s+$/g, ""));
        blocks.push(parts.join("\n"));
      }
      if ((tab.state === "sending" || tab.state === "failed") && tab.cmd) {
        const parts = [`> ${tab.cmd}`];
        if (tab.error) parts.push(String(tab.error).replace(/\s+$/g, ""));
        blocks.push(parts.join("\n"));
      }
      return blocks.join("\n\n");
    }

    const consoleCanCopy = computed(
      () => !!consoleTranscriptText(consoleActive.value),
    );

    async function copyConsoleHistory() {
      const text = consoleTranscriptText(consoleActive.value);
      if (!text) return;
      try {
        if (!navigator.clipboard?.writeText) throw new Error("no clipboard");
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      consoleCopied.value = true;
      window.clearTimeout(consoleCopyTimer);
      consoleCopyTimer = window.setTimeout(() => {
        consoleCopied.value = false;
      }, 1200);
    }

    async function savePendingCmd(tab, item) {
      const cmd = String(item.cmd || "").trim();
      if (!cmd) return;
      try {
        const ev = await apiPatchPending(tab.tab_id, item.id, cmd);
        upsertConsoleTab(ev, tab.draft);
      } catch (err) {
        console.error(err);
      }
    }

    async function dropPendingCmd(tab, item) {
      try {
        const ev = await apiDeletePending(tab.tab_id, item.id);
        upsertConsoleTab(ev, tab.draft);
      } catch (err) {
        console.error(err);
      }
    }

    async function clearPendingAll() {
      const tab = consoleActive.value;
      if (!tab) return;
      try {
        const ev = await apiDeletePending(tab.tab_id);
        upsertConsoleTab(ev, tab.draft);
      } catch (err) {
        console.error(err);
      }
    }

    async function reconcileConsole(snap) {
      const serverTabs = snap?.poll?.console?.tabs;
      if (Array.isArray(serverTabs) && serverTabs.length) {
        const drafts = Object.fromEntries(
          consoleTabs.map((t) => [t.tab_id, t.draft]),
        );
        const stored = loadConsoleStore();
        for (const t of stored.tabs || []) {
          if (t.tab_id && t.draft && drafts[t.tab_id] == null)
            drafts[t.tab_id] = t.draft;
        }
        for (const ev of serverTabs) upsertConsoleTab(ev, drafts[ev.tab_id]);
        const ids = new Set(serverTabs.map((t) => t.tab_id));
        if (!consoleActiveId.value || !ids.has(consoleActiveId.value)) {
          consoleActiveId.value = serverTabs[0].tab_id;
        }
        persistConsole();
        consoleBootstrapped = true;
        return;
      }
      if (consoleBootstrapped || consoleSeeding) return;
      const stored = loadConsoleStore();
      if (!stored.tabs?.length) {
        consoleBootstrapped = true;
        return;
      }
      consoleSeeding = true;
      try {
        for (const t of stored.tabs) {
          if (!t.tab_id || !t.key) continue;
          if (!consoleTabs.some((x) => x.tab_id === t.tab_id)) {
            consoleTabs.push({
              tab_id: t.tab_id,
              key: t.key,
              state: "ready",
              history: t.history || [],
              pending: t.pending || [],
              draft: t.draft || "",
              unread: !!t.unread,
              cmds: Array.isArray(t.cmds) ? t.cmds : cmdsFromHistory(t.history),
              cmdIndex: null,
              cmdHold: "",
            });
          }
          try {
            await apiOpenConsole(t.key, t.tab_id);
            if (t.failed) await apiParkConsole(t.tab_id, t.failed, t.error);
            else if (t.inflight) await apiSendConsole(t.tab_id, t.inflight);
            for (const p of t.pending || []) {
              if (p?.cmd) await apiSendConsole(t.tab_id, p.cmd);
            }
          } catch (err) {
            console.error(err);
          }
        }
        consoleActiveId.value = stored.active || consoleTabs[0]?.tab_id || null;
        persistConsole();
        consoleBootstrapped = true;
      } finally {
        consoleSeeding = false;
      }
    }

    function consoleFromLocation() {
      return new URLSearchParams(location.search).get("console") === "1";
    }

    async function maybeReopenConsole() {
      if (!consoleFromLocation()) return;
      const ctab = new URLSearchParams(location.search).get("ctab");
      if (ctab && consoleTabs.some((t) => t.tab_id === ctab))
        consoleActiveId.value = ctab;
      await showConsole();
    }

    /** @param {Record<string, unknown> | null | undefined} ota */
    function otaLocalLabel(ota) {
      if (!ota || typeof ota !== "object") return "—";
      const local =
        /** @type {{ state?: string, mid?: string, pct?: number, have?: number, total?: number, age_s?: number, status_word?: string }} */ (
          ota
        ).local;
      if (!local || !local.state || local.state === "none")
        return "no download";
      const bits = [];
      if (local.status_word) bits.push(local.status_word);
      else bits.push(local.state);
      if (local.have != null && local.total != null)
        bits.push(`${local.have}/${local.total}`);
      if (local.pct != null) bits.push(`${local.pct}%`);
      if (local.mid) bits.push(`id=${local.mid}`);
      if (local.age_s != null) bits.push(`${local.age_s}s`);
      return bits.join(" ");
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaHwLabel(running) {
      if (!running || typeof running !== "object") return "—";
      const hw = /** @type {{ hw_id?: string | null }} */ (running).hw_id;
      if (hw == null || hw === "") return "—";
      return hw;
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaTargetLabel(running) {
      if (!running || typeof running !== "object") return "—";
      const r = /** @type {{ target_env?: string, target_id?: string }} */ (
        running
      );
      const env = r.target_env && r.target_env !== "?" ? r.target_env : "";
      if (env && r.target_id) return `${env} (${r.target_id})`;
      return env || r.target_id || "—";
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function otaBodyLabel(unit) {
      if (!unit) return "—";
      const running =
        unit.ota && typeof unit.ota === "object"
          ? /** @type {{ running?: { body_hash?: string, image_kib?: number } }} */ (
              unit.ota
            ).running
          : null;
      const full = typeof unit.base_hash === "string" ? unit.base_hash : "";
      const prefix = running?.body_hash || "";
      const hash = full || prefix || "";
      if (!hash) return "—";
      const kib = running?.image_kib;
      return kib != null ? `${hash} (${kib}K)` : hash;
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaServingLabel(running) {
      if (!running || typeof running !== "object" || !("serving" in running))
        return "—";
      const r = /** @type {{ serving?: boolean, serving_count?: number }} */ (
        running
      );
      const on = r.serving ? "on" : "off";
      return r.serving_count != null ? `${on} (${r.serving_count})` : on;
    }

    /** @param {Record<string, unknown> | null | undefined} running */
    function otaBlLabel(running) {
      if (!running || typeof running !== "object") return "";
      const r = /** @type {{ bl_rc?: string }} */ (running);
      return r.bl_rc ? `rc=${r.bl_rc}` : "";
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function canInstallUnit(unit) {
      if (!unit || isInFlight(unit) || !manualAccepting.value) return false;
      const ota = unit.ota;
      if (!ota || typeof ota !== "object") return false;
      const local = /** @type {{ state?: string }} */ (ota).local;
      const running = /** @type {{ bl_apply?: boolean }} */ (ota).running;
      if (local?.state !== "ready") return false;
      if (running && running.bl_apply === false) return false;
      return true;
    }

    /** @param {Record<string, unknown> | undefined} unit @param {Record<string, unknown>} row */
    function canStageRow(unit, row) {
      if (!unit || isInFlight(unit) || !manualAccepting.value) return false;
      const ota = unit.ota;
      const local =
        ota && typeof ota === "object"
          ? /** @type {{ state?: string }} */ (ota).local
          : null;
      if (local?.state === "downloading" || local?.state === "ready")
        return false;
      return typeof row.index === "number" || typeof row.index === "string";
    }

    /** @param {Record<string, unknown>} unit @param {'refresh' | 'pull' | 'push' | 'stage' | 'install'} job */
    function markOptimistic(unit, job) {
      const key = String(unit.key);
      const prev = fleet.units[key] || unit;
      const stateMap = {
        refresh: "refreshing",
        pull: "pulling",
        push: "pushing",
        stage: "staging",
        install: "installing",
      };
      const state = stateMap[job] || "polling";
      fleet.units[key] = {
        ...prev,
        session: {
          ...(typeof prev.session === "object" && prev.session
            ? prev.session
            : {}),
          state,
          stage: "Logging in",
          kind: "login",
          manual: true,
          job,
        },
      };
    }

    /** @param {Record<string, unknown>} unit @param {'refresh' | 'pull' | 'push'} job @param {Event} [ev] */
    async function runManualJob(unit, job, ev) {
      ev?.stopPropagation?.();
      if (!canManualUnit(unit, job)) return;
      markOptimistic(unit, job);
      pushMap();
      const fn =
        job === "refresh" ? refreshUnit : job === "pull" ? pullUnit : pushUnit;
      try {
        const updated = await fn(String(unit.key));
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    /** @param {Record<string, unknown>} unit @param {Record<string, unknown>} row @param {Event} [ev] */
    async function runStage(unit, row, ev) {
      ev?.stopPropagation?.();
      if (!canStageRow(unit, row)) return;
      markOptimistic(unit, "stage");
      pushMap();
      try {
        const updated = await stageUnit(String(unit.key), row.index);
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    /** @param {Record<string, unknown>} unit @param {Event} [ev] */
    async function runInstall(unit, ev) {
      ev?.stopPropagation?.();
      if (!canInstallUnit(unit)) return;
      const local =
        unit.ota && typeof unit.ota === "object"
          ? /** @type {{ mid?: string }} */ (unit.ota).local
          : null;
      const mid = local?.mid || "staged update";
      if (
        !window.confirm(
          `Install OTA on ${unit.label || unit.key}? Node will reboot (${mid}). Wrong image can brick until USB recovery.`,
        )
      ) {
        return;
      }
      markOptimistic(unit, "install");
      pushMap();
      try {
        const updated = await installUnit(String(unit.key));
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    const activeCount = computed(
      () => Object.values(fleet.units || {}).filter(isInFlight).length,
    );

    const attentionCount = computed(
      () =>
        Object.values(fleet.units || {}).filter((u) => {
          const h = healthHeadline(u, fleet.now);
          return h === "attention" || h === "unreachable";
        }).length,
    );

    const pausedCount = computed(
      () => Object.values(fleet.units || {}).filter((u) => !!u.paused).length,
    );

    /** @param {Record<string, unknown>} unit */
    function matchesListFilter(unit) {
      const f = listFilter.value;
      if (f === "active") return isInFlight(unit);
      if (f === "attention") {
        const h = healthHeadline(unit, fleet.now);
        return h === "attention" || h === "unreachable";
      }
      if (f === "paused") return !!unit.paused;
      return true;
    }

    const listFilterEmpty = computed(() => {
      if (listFilter.value === "active") return "Nothing in flight.";
      if (listFilter.value === "attention") return "Nothing needs attention.";
      if (listFilter.value === "paused") return "No paused units.";
      return "No units.";
    });

    const sortedUnits = computed(() => {
      const q = search.value.trim().toLowerCase();
      let units = Object.values(fleet.units || {}).filter(matchesListFilter);
      if (q) {
        units = units.filter((u) => {
          const hay = [
            u.key,
            u.unit_id,
            u.site,
            u.site_name,
            u.alias,
            u.label,
            u.notes,
          ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
          return hay.includes(q);
        });
      }
      return units.sort(compareUnits);
    });

    const selectedUnit = computed(() =>
      selectedKey.value ? fleet.units[selectedKey.value] : null,
    );

    const unitHistory = computed(() => selectedUnit.value?.history || null);

    async function loadHistoriesFor(key) {
      try {
        const res = await fetchPolls(key, historyHours);
        loadHistories(key, res.histories || {});
      } catch {
        clearHistories(key);
      }
    }

    const sparkSeries = computed(() =>
      seriesFromHistories(unitHistory.value || {}),
    );

    const sparkDomain = computed(() => {
      const now = Math.floor(Date.now() / 1000);
      return { tMin: now - historyHours * 3600, tMax: now };
    });

    const sparkModels = computed(() => {
      /** @type {Record<string, ReturnType<typeof sparklineWallTime>>} */
      const out = {};
      const domain = sparkDomain.value;
      for (const row of METRIC_ROWS) {
        const wave = !!row.wave;
        out[row.key] = sparklineWallTime(
          sparkSeries.value[row.key] || [],
          168,
          22,
          {
            minSpan: SPARK_MIN_SPAN[row.key] || 0,
            tMin: domain.tMin,
            tMax: domain.tMax,
            ...(wave ? { yMin: -90, yMax: 90, dots: false } : {}),
          },
        );
      }
      return out;
    });

    const SPARK_VALUE_FORMAT = {
      battery_mv: (v) => `${v.toFixed(3)} V`,
      temperature: (v) => formatTemp(v),
      unreadable_pct: (v) => `${v.toFixed(1)}%`,
      recv_rate: (v) => `${v.toFixed(1)}/h`,
      noise_floor: (v) => `${Math.trunc(v)} dBm`,
    };

    function metricNow(metric) {
      const unit = selectedUnit.value;
      if (metric === "sun") {
        const points = sparkSeries.value.sun || [];
        const last = points[points.length - 1];
        if (!last || last.value == null) return "—";
        const elev = Number(last.value);
        return `${elev >= 0 ? "☀️" : "🌙"} ${elev.toFixed(0)}∠`;
      }
      if (metric === "battery_mv") {
        const mv = unit?.status?.battery_mv;
        if (mv != null) return formatBattery(mv);
      }
      if (metric === "temperature") {
        const t = unit?.telemetry?.temperature;
        if (t != null) return formatTemp(t);
      }
      const points = sparkSeries.value[metric] || [];
      const last = [...points]
        .reverse()
        .find((p) => p.value != null && Number.isFinite(Number(p.value)));
      if (!last) return "—";
      const fmt = SPARK_VALUE_FORMAT[metric] || ((v) => String(v));
      return fmt(Number(last.value));
    }

    /** Latest value as text when there are too few points for a line. */
    function sparkFallback(metric) {
      return metricNow(metric);
    }

    function formatPollDelta(recv, sent) {
      const parts = [];
      if (recv != null) parts.push(`+${recv}`);
      if (sent != null) parts.push(`+${sent}`);
      return parts.length ? parts.join(" / ") : "—";
    }

    function formatPollVoltage(poll) {
      if (poll?.battery_mv != null) return formatBattery(poll.battery_mv);
      return "—";
    }

    function formatPollTemp(poll) {
      return poll?.temperature != null ? formatTemp(poll.temperature) : "—";
    }

    function voltageStock(poll) {
      return formatSignedDelta(poll?.delta_voltage, 2);
    }

    function tempStock(poll) {
      const d = poll?.delta_temperature;
      if (d == null || !Number.isFinite(Number(d))) return null;
      return formatSignedDelta((Number(d) * 9) / 5, 0);
    }

    function unitKeyFromLocation() {
      const raw = new URLSearchParams(location.search).get("unit");
      if (!raw) return null;
      const needle = raw.trim();
      if (!needle) return null;
      if (fleet.units[needle]) return needle;
      const lower = needle.toLowerCase();
      if (fleet.units[lower]) return lower;
      for (const [k, u] of Object.entries(fleet.units || {})) {
        if (k.toLowerCase() === lower) return k;
        if (String(u.unit_id || "").toLowerCase() === lower) return k;
      }
      return lower;
    }

    function syncLocation(key) {
      const url = new URL(location.href);
      if (key) url.searchParams.set("unit", key);
      else url.searchParams.delete("unit");
      if (consoleOpen.value) {
        url.searchParams.set("console", "1");
        if (consoleActiveId.value)
          url.searchParams.set("ctab", consoleActiveId.value);
        else url.searchParams.delete("ctab");
      } else {
        url.searchParams.delete("console");
        url.searchParams.delete("ctab");
      }
      if (mapOpen.value) url.searchParams.set("map", "1");
      else url.searchParams.delete("map");
      const next = `${url.pathname}${url.search}${url.hash}`;
      const cur = `${location.pathname}${location.search}${location.hash}`;
      if (next === cur) return;
      history.replaceState(null, "", next);
    }

    function openDetail(key, { fly = false } = {}) {
      hideConsole();
      selectedKey.value = key;
      mapHighlightKey.value = key;
      syncLocation(key);
      if (fly && mapCtrl) mapCtrl.flyTo(key, fleet);
      pushMap();
      loadHistoriesFor(key);
    }

    function selectFromDashboard(key) {
      if (selectedKey.value === key) {
        clearSelection();
        return;
      }
      openDetail(key);
    }

    function selectFromMapPin(key) {
      if (selectedKey.value === key) {
        clearSelection();
        return;
      }
      openDetail(key, { fly: true });
    }

    async function applyLocationUnit() {
      const key = unitKeyFromLocation();
      if (!key) {
        if (selectedKey.value) {
          await clearSelection();
        }
        return;
      }
      if (!fleet.units[key]) return;
      if (selectedKey.value === key) return;
      openDetail(key);
    }

    async function selectUnit(key) {
      await selectFromDashboard(key);
    }

    function hideConsoleAtEvent(ev) {
      const key = mapCtrl?.hitUnit?.(ev.clientX, ev.clientY);
      if (key) {
        selectFromMapPin(key);
        return;
      }
      hideConsole();
    }

    function hideMapAtEvent(ev) {
      const key = mapCtrl?.hitUnit?.(ev.clientX, ev.clientY);
      if (key) {
        selectFromMapPin(key);
        return;
      }
      hideMap();
    }

    async function clearSelection() {
      selectedKey.value = null;
      mapHighlightKey.value = null;
      syncLocation(null);
      pushMap();
      await nextTick();
      mapCtrl?.resize?.();
    }

    const modalDownOnBackdrop = ref(false);

    function onModalBackdropMouseDown() {
      modalDownOnBackdrop.value = true;
    }

    function onModalBackdropMouseUp() {
      if (modalDownOnBackdrop.value) clearSelection();
      modalDownOnBackdrop.value = false;
    }

    function onModalPanelMouseUp() {
      modalDownOnBackdrop.value = false;
    }

    /** @param {Record<string, unknown> | undefined} unit */
    function sessionBadgeTitle(unit) {
      const s = unit?.session;
      if (!s || typeof s !== "object") return "";
      const parts = [];
      const attempt = s.attempt;
      const max = s.max_attempts;
      if (
        typeof attempt === "number" &&
        attempt > 0 &&
        typeof max === "number" &&
        max > 0
      ) {
        parts.push(`${attempt}/${max}`);
      } else if (typeof attempt === "number" && attempt > 0) {
        parts.push(`attempt ${attempt}`);
      }
      if (typeof s.error === "string" && s.error) parts.push(s.error);
      return parts.join(" · ");
    }

    function cardPrimary(unit) {
      return unitLabel(unit);
    }

    function cardNodeId(unit) {
      return String(unit.unit_id || unit.key || "");
    }

    function cardShowNodeId(unit) {
      const id = cardNodeId(unit);
      return !!id && id !== cardPrimary(unit);
    }

    /** Short OTA chip on dashboard cards; full text in title. */
    function cardOtaLabel(unit) {
      const badge = unit?.ota_badge;
      if (typeof badge !== "string" || !badge) return null;
      if (badge === "sees update") return "fw";
      if (badge === "downloading") return "dl";
      return badge;
    }

    function cardVoltage(unit) {
      const mv = unit?.status?.battery_mv;
      if (mv != null) return formatBattery(mv);
      const pts = unit?.sparks?.battery_mv || [];
      const last = pts[pts.length - 1];
      if (last?.value != null && Number.isFinite(Number(last.value))) {
        return `${Number(last.value).toFixed(3)} V`;
      }
      return "—";
    }

    function cardTraffic(unit) {
      const pts = unit?.sparks?.recv_rate || [];
      const last = [...pts]
        .reverse()
        .find((p) => p.value != null && Number.isFinite(Number(p.value)));
      if (!last) return "—";
      return `${Number(last.value).toFixed(1)}/h`;
    }

    function cardTemp(unit) {
      const t = unit?.telemetry?.temperature;
      if (t != null) return formatTemp(t);
      const pts = unit?.sparks?.temperature || [];
      const last = pts[pts.length - 1];
      if (last?.value != null && Number.isFinite(Number(last.value))) {
        return formatTemp(Number(last.value));
      }
      return "—";
    }

    function cardError(unit) {
      const pts = unit?.sparks?.unreadable_pct || [];
      const last = [...pts]
        .reverse()
        .find((p) => p.value != null && Number.isFinite(Number(p.value)));
      if (last) return `${Number(last.value).toFixed(1)}%`;
      const st = unit?.status;
      if (st?.recv_errors != null && st?.packets_recv != null) {
        const total = Number(st.packets_recv) + Number(st.recv_errors);
        if (total > 0)
          return `${((Number(st.recv_errors) / total) * 100).toFixed(1)}%`;
      }
      return "—";
    }

    function cardMetricValue(unit, metric) {
      if (metric === "battery_mv") return cardVoltage(unit);
      if (metric === "temperature") return cardTemp(unit);
      if (metric === "recv_rate") return cardTraffic(unit);
      if (metric === "unreadable_pct") return cardError(unit);
      return "—";
    }

    const dashboardSparkDomain = computed(() => {
      const now = Math.floor(Date.now() / 1000);
      return { tMin: now - historyHours * 3600, tMax: now };
    });

    function dashboardSparkModel(unit, metric) {
      const sparks = unit?.sparks || {};
      const points = sparks[metric] || [];
      const domain = dashboardSparkDomain.value;
      return sparklineWallTime(points, DASH_SPARK_W, DASH_SPARK_H, {
        minSpan: SPARK_MIN_SPAN[metric] || 0,
        tMin: domain.tMin,
        tMax: domain.tMax,
      });
    }

    function dashboardSparkHasData(unit, metric) {
      const m = dashboardSparkModel(unit, metric);
      return !!(m && (m.line || (m.dots && m.dots.length)));
    }

    const DASH_METRICS = [
      { key: "battery_mv", label: "V", stroke: "#6ee7a0" },
      { key: "temperature", label: "Temp", stroke: "#f0b86e" },
      { key: "recv_rate", label: "In/h", stroke: "#4ea1ff" },
      { key: "unreadable_pct", label: "Err", stroke: "#f06e6e" },
    ];

    function syncBookDrafts(unit) {
      if (!unit || aliasEditing.value || notesEditing.value) return;
      aliasDraft.value = typeof unit.alias === "string" ? unit.alias : "";
      notesDraft.value = typeof unit.notes === "string" ? unit.notes : "";
    }

    watch(selectedKey, (key) => {
      aliasEditing.value = false;
      notesEditing.value = false;
      resetLogShown();
      syncBookDrafts(selectedUnit.value);
      syncLocation(key);
    });

    watch(consoleActiveId, (id) => {
      persistConsole();
      if (consoleOpen.value) {
        if (id) seeConsoleTab(id);
        syncLocation(selectedKey.value);
      }
    });

    watch(
      () => selectedUnit.value?.alias,
      () => syncBookDrafts(selectedUnit.value),
    );
    watch(
      () => selectedUnit.value?.notes,
      () => syncBookDrafts(selectedUnit.value),
    );

    /** @param {Record<string, unknown>} unit */
    async function saveAlias(unit) {
      aliasEditing.value = false;
      const next = aliasDraft.value.trim();
      const prev = typeof unit.alias === "string" ? unit.alias : "";
      if (next === prev) return;
      try {
        const updated = await patchUnit(String(unit.key), {
          alias: next || null,
        });
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
        aliasDraft.value = prev;
      }
    }

    /** @param {Record<string, unknown>} unit */
    async function saveNotes(unit) {
      notesEditing.value = false;
      const next = notesDraft.value;
      const prev = typeof unit.notes === "string" ? unit.notes : "";
      if (next === prev) return;
      try {
        const updated = await patchUnit(String(unit.key), {
          notes: next.trim() ? next : null,
        });
        applyUnit(updated);
      } catch (err) {
        console.error(err);
        notesDraft.value = prev;
      }
    }

    async function togglePublic(unit, ev) {
      try {
        const updated = await patchUnit(unit.key, {
          public: ev.target.checked,
        });
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    /** @param {Record<string, unknown>} unit */
    function effectiveRoutingPolicy(unit) {
      const explicit = unit.routing_explicit;
      if (explicit === "direct" || explicit === "flood") return explicit;
      return "path";
    }

    /** @param {Record<string, unknown>} unit */
    function routingPolicyLabel(unit) {
      const p = effectiveRoutingPolicy(unit);
      return p === "path" ? "path (default)" : p;
    }

    /** @param {Record<string, unknown>} unit */
    function liveRouteLabel(unit) {
      const lr = unit.live_route;
      if (!lr || typeof lr !== "object") return "unknown";
      return String(lr.label || "unknown");
    }

    /** @param {Record<string, unknown>} unit */
    function liveRouteTitle(unit) {
      const lr = unit.live_route;
      if (!lr || typeof lr !== "object") return "Route unknown";
      if (lr.kind === "flood" && lr.fallback) return "Rediscovering route";
      if (lr.kind === "flood") return "Flood route";
      return `Live route: ${lr.label}`;
    }

    /** @param {Record<string, unknown>} unit */
    function liveRouteBadgeClass(unit) {
      const lr = unit.live_route;
      if (!lr || typeof lr !== "object") return "live-route-badge live-route-unknown";
      if (lr.kind === "flood" && lr.fallback) {
        return "live-route-badge live-route-flood-fallback";
      }
      if (lr.kind === "flood") return "live-route-badge live-route-flood";
      return "live-route-badge";
    }

    /** @param {Record<string, unknown>} unit */
    function showPolicyFloodBadge(unit) {
      return unit.routing_explicit === "flood";
    }

    /** @param {Record<string, unknown>} unit @param {'path'|'direct'|'flood'} mode */
    async function setRoutingPolicy(unit, mode) {
      try {
        const updated = await patchUnit(String(unit.key), {
          routing: mode === "path" ? null : mode,
        });
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    /** @param {Record<string, unknown>} unit @param {Event} [ev] */
    async function togglePaused(unit, ev) {
      ev?.stopPropagation?.();
      const next =
        ev && ev.target && "checked" in ev.target
          ? !!ev.target.checked
          : !unit.paused;
      try {
        const updated = await patchUnit(String(unit.key), { paused: next });
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    async function ackStability(unit, ev) {
      ev?.stopPropagation?.();
      try {
        const updated = await patchUnit(String(unit.key), { ack_stability: true });
        applyUnit(updated);
        pushMap();
      } catch (err) {
        console.error(err);
      }
    }

    function pushMap() {
      mapCtrl?.sync(fleet, selectedKey.value ?? mapHighlightKey.value, fleet.now);
    }

    /** @param {EventTarget | null} target */
    function isTypingTarget(target) {
      const el = /** @type {HTMLElement | null} */ (target);
      if (!el) return false;
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (el.isContentEditable) return true;
      return false;
    }

    function focusSearchAtEnd() {
      const el = searchInput.value;
      if (!el) return;
      el.focus();
      const len = el.value.length;
      el.setSelectionRange(len, len);
    }

    function appendToSearch(ch) {
      search.value += ch;
      nextTick(() => focusSearchAtEnd());
    }

    function clearSearch() {
      if (!search.value) return false;
      search.value = "";
      nextTick(() => focusSearchAtEnd());
      return true;
    }

    onMounted(async () => {
      startClock();
      const onKey = (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "f") {
          e.preventDefault();
          searchInput.value?.focus();
          searchInput.value?.select();
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          if (consoleOpen.value) {
            hideConsole();
            return;
          }
          if (selectedKey.value) {
            clearSelection();
            return;
          }
          if (mapOpen.value) {
            hideMap();
            return;
          }
          if (search.value) {
            clearSearch();
          }
          return;
        }
        if (
          !consoleOpen.value &&
          !isTypingTarget(e.target) &&
          !e.metaKey &&
          !e.ctrlKey &&
          !e.altKey &&
          e.key.length === 1
        ) {
          e.preventDefault();
          appendToSearch(e.key);
        }
      };
      const onPageHide = () => persistConsole();
      window.addEventListener("pagehide", onPageHide);
      const onPop = () => applyLocationUnit();
      window.addEventListener("keydown", onKey);
      window.addEventListener("popstate", onPop);
      onUnmounted(() => {
        window.removeEventListener("keydown", onKey);
        window.removeEventListener("popstate", onPop);
        window.removeEventListener("pagehide", onPageHide);
      });
      try {
        await applyHello(await fetchFleet());
        applyLocationUnit();
        await maybeReopenConsole();
        await maybeReopenMap();
      } catch (err) {
        console.error(err);
      }
      watch(
        () => fleet.now,
        () => pushMap(),
      );
      es = connectEvents({
        onHello: async (snap) => {
          await applyHello(snap);
          applyLocationUnit();
          pushMap();
        },
        onUnit: (unit) => {
          applyUnit(unit);
          pushMap();
        },
        onSession: (poll) => {
          applySession(poll);
        },
        onConsole: (event) => {
          applyConsoleEvent(event);
        },
      });
      stopBenchGeo = startBenchGeolocation();
    });

    watch(search, () => {});

    watch([selectedKey, mapOpen], () => {
      nextTick(() => mapCtrl?.resize?.());
    });

    onUnmounted(() => {
      stopClock();
      stopBenchGeo?.();
      es?.close();
      mapCtrl?.destroy();
    });

    return {
      fleet,
      search,
      searchInput,
      listFilter,
      activeCount,
      attentionCount,
      pausedCount,
      listFilterEmpty,
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
      healthEmoji,
      showHealthMark,
      healthTooltip,
      cardPrimary,
      cardNodeId,
      cardShowNodeId,
      cardOtaLabel,
      cardVoltage,
      cardTraffic,
      cardTemp,
      cardError,
      cardMetricValue,
      dashboardSparkHasData,
      dashboardSparkModel,
      DASH_METRICS,
      DASH_SPARK_W,
      DASH_SPARK_H,
      mapOpen,
      mapHighlightKey,
      toggleMap,
      hideMap,
      hideMapAtEvent,
      selectFromDashboard,
      aliasDraft,
      notesDraft,
      saveAlias,
      saveNotes,
      onAliasFocus: () => {
        aliasEditing.value = true;
      },
      onNotesFocus: () => {
        notesEditing.value = true;
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
      ackStability,
      setRoutingPolicy,
      effectiveRoutingPolicy,
      routingPolicyLabel,
      liveRouteLabel,
      liveRouteTitle,
      liveRouteBadgeClass,
      showPolicyFloodBadge,
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
      consoleOpen,
      consoleTabs,
      consoleActive,
      consoleActiveId,
      consoleBusy,
      consoleFailed,
      consoleStatusLine,
      consoleBadgeBusy,
      consoleUnread,
      consoleScroll,
      consolePickerSearch,
      consolePickerOpen,
      consolePickerQuery,
      consolePickerUnits,
      toggleConsolePicker,
      consoleTabLabel,
      selectConsoleTab,
      toggleConsole,
      openConsoleForUnit,
      unitConsoleUnread,
      unitConsoleBusy,
      hideConsole,
      hideConsoleAtEvent,
      addConsoleTab,
      closeConsoleTab,
      clearConsoleHistory,
      copyConsoleHistory,
      consoleCanCopy,
      consoleCopied,
      submitConsoleLine,
      recallConsoleCmd,
      onConsoleDraftInput,
      cancelConsoleSend,
      retryConsoleSend,
      retryConsoleCmd,
      consoleHistoryRetry,
      skipConsoleFailed,
      savePendingCmd,
      dropPendingCmd,
      clearPendingAll,
    };
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
            ref="searchInput"
            v-model="search"
            class="search-input"
            type="search"
            placeholder="Search site, unit, name…"
            autocomplete="off"
            spellcheck="false"
          />
        </label>
      </div>
      <div class="header-actions">
      <button
        type="button"
        class="map-launch console-launch"
        :class="{ 'is-open': mapOpen }"
        title="Map"
        aria-label="Open map"
        @click="toggleMap"
      >
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7">
          <path d="M3 6.5l7-3.5 7 3.5v7L10 17 3 13.5v-7z" stroke-linejoin="round" />
          <path d="M10 3v14M3 6.5l7 3.5 7-3.5" stroke-linejoin="round" />
        </svg>
      </button>
      <button
        type="button"
        class="console-launch"
        :class="{ 'is-open': consoleOpen, 'is-busy': consoleBadgeBusy, 'is-unread': consoleUnread }"
        :title="consoleTabs.length ? 'Console (' + consoleTabs.length + ')' : 'Console'"
        :aria-label="consoleUnread ? 'Open console (unread)' : 'Open console'"
        @click="toggleConsole"
      >
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7">
          <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
          <path d="M6 8.5l2.2 1.6L6 11.7" stroke-linecap="round" stroke-linejoin="round" />
          <path d="M10.2 12.4H14" stroke-linecap="round" />
        </svg>
        <span v-if="consoleUnread" class="console-launch-unread" aria-hidden="true">*</span>
        <span v-if="consoleTabs.length" class="console-launch-badge">{{ consoleTabs.length }}</span>
      </button>
      </div>
    </header>
    <main id="dashboard">
      <div class="dashboard-toolbar">
        <div class="list-filter" role="tablist" aria-label="Dashboard filter">
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
          <button
            type="button"
            role="tab"
            :class="{ on: listFilter === 'attention' }"
            :aria-selected="listFilter === 'attention'"
            @click="listFilter = 'attention'"
          >
            Attention{{ attentionCount ? ' ' + attentionCount : '' }}
          </button>
          <button
            type="button"
            role="tab"
            :class="{ on: listFilter === 'paused' }"
            :aria-selected="listFilter === 'paused'"
            @click="listFilter = 'paused'"
          >
            Paused{{ pausedCount ? ' ' + pausedCount : '' }}
          </button>
        </div>
      </div>
      <div class="dashboard-grid">
        <p v-if="!sortedUnits.length" class="list-empty">
          {{ listFilterEmpty }}
        </p>
        <article
          v-for="unit in sortedUnits"
          :key="unit.key"
          class="dash-card"
          :class="{ selected: unit.key === selectedKey, paused: !!unit.paused, busy: isInFlight(unit) }"
          @click="selectFromDashboard(unit.key)"
        >
          <div class="dash-head">
            <span class="dash-title" :title="healthTooltip(unit.health)">
              <span
                v-if="showHealthMark(unit)"
                class="dash-health-badge"
                :class="'dash-health-' + healthHeadline(unit, fleet.now)"
                :title="healthTooltip(unit.health)"
                :aria-label="healthMark(unit, fleet.now)"
              >{{ healthEmoji(unit, fleet.now) }}</span>
              <span class="unit-name" :class="'unit-name-' + healthHeadline(unit, fleet.now)">{{ cardPrimary(unit) }}</span>
            </span>
            <div class="unit-actions">
              <button
                type="button"
                class="unit-console"
                :class="{ 'is-unread': unitConsoleUnread(unit), 'is-busy': unitConsoleBusy(unit) }"
                title="Console"
                :aria-label="unitConsoleUnread(unit) ? 'Open console (unread)' : 'Open console'"
                @click.stop="openConsoleForUnit(unit, $event)"
              >
                <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7">
                  <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
                  <path d="M6 8.5l2.2 1.6L6 11.7" stroke-linecap="round" stroke-linejoin="round" />
                  <path d="M10.2 12.4H14" stroke-linecap="round" />
                </svg>
                <span v-if="unitConsoleUnread(unit)" class="unit-console-unread" aria-hidden="true">*</span>
              </button>
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
          </div>
          <div class="dash-foot" :class="{ busy: isInFlight(unit) }">
            <span
              v-if="isInFlight(unit)"
              class="dash-foot-busy"
              :title="sessionBadgeTitle(unit)"
            >{{ unitStage(unit) }}</span>
            <span v-else class="dash-foot-meta">
              <span v-if="cardShowNodeId(unit)" class="unit-id">{{ cardNodeId(unit) }}</span>
              <span
                v-if="showPolicyFloodBadge(unit)"
                class="routing-policy-danger"
                title="Always flood — high airtime"
              >flood</span>
              <span
                v-if="unit.routing_explicit === 'direct'"
                class="routing-policy-direct"
                title="Direct override"
              >direct*</span>
              <span
                v-if="cardOtaLabel(unit)"
                class="ota-list-badge ota-card-badge"
                :class="unit.ota_badge ? 'ota-badge-' + unit.ota_badge.replace(' ', '-') : ''"
                :title="unit.ota_badge"
              >{{ cardOtaLabel(unit) }}</span>
            </span>
            <span class="dash-foot-ago">{{ formatRelative(unit.last_heard, fleet.now) }}</span>
          </div>
          <div class="dash-metrics">
            <div v-for="row in DASH_METRICS" :key="row.key" class="dash-metric">
              <span class="dash-metric-val">{{ cardMetricValue(unit, row.key) }}</span>
              <svg
                v-if="dashboardSparkHasData(unit, row.key)"
                class="dash-spark"
                :width="DASH_SPARK_W"
                :height="DASH_SPARK_H"
                :viewBox="'0 0 ' + DASH_SPARK_W + ' ' + DASH_SPARK_H"
                aria-hidden="true"
              >
                <polyline
                  v-if="dashboardSparkModel(unit, row.key).line"
                  fill="none"
                  :stroke="row.stroke"
                  stroke-width="1.4"
                  :points="dashboardSparkModel(unit, row.key).line"
                />
                <circle
                  v-for="(dot, di) in dashboardSparkModel(unit, row.key).dots"
                  :key="di"
                  :cx="dot.x"
                  :cy="dot.y"
                  r="1.4"
                  :fill="row.stroke"
                />
              </svg>
            </div>
          </div>
        </article>
      </div>
    </main>
    <Teleport :to="mapOpen ? '#map-detail-slot' : 'body'">
    <div
      v-if="selectedUnit"
      :class="mapOpen ? 'map-detail-host' : 'detail-modal overlay-modal'"
      @mousedown.self="!mapOpen && onModalBackdropMouseDown()"
      @mouseup.self="!mapOpen && onModalBackdropMouseUp()"
    >
          <div
            id="detail"
            class="detail"
            :class="{ 'detail-map-side': mapOpen }"
            role="dialog"
            aria-modal="true"
            aria-labelledby="detail-title"
            @mouseup="onModalPanelMouseUp"
          >
          <div class="detail-head">
            <h2 id="detail-title">{{ unitTitle(selectedUnit) }}</h2>
            <button type="button" class="detail-close" aria-label="Close" @click="clearSelection">×</button>
          </div>
          <p class="sub">
            {{ selectedUnit.unit_id }}
            · {{ selectedUnit.public ? 'public' : 'private' }}
            · {{ formatRelative(selectedUnit.last_heard, fleet.now) }}
            <span v-if="selectedUnit.drift"> · {{ selectedUnit.drift }}</span>
          </p>
          <p v-if="selectedUnit.prefs?.length" class="pref-line">
            <span
              v-for="pref in selectedUnit.prefs"
              :key="pref.id"
              class="pref-badge"
              :class="'pref-' + pref.state"
              :title="pref.state === 'due' ? 'Book apply due' : 'Apply stamped'"
            >{{ pref.label }} {{ pref.value }}</span>
          </p>
          <p class="live-route-line">
            <span class="book-field-label">Route</span>
            <span :class="liveRouteBadgeClass(selectedUnit)" :title="liveRouteTitle(selectedUnit)">
              {{ liveRouteLabel(selectedUnit) }}
              <span
                v-if="selectedUnit.live_route?.kind === 'flood' && selectedUnit.live_route?.fallback"
                class="live-route-sub"
              >discovering</span>
            </span>
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
          <section class="routing-group">
            <span class="book-field-label">Routing policy</span>
            <div class="routing-segment" role="group" aria-label="Routing policy">
              <button
                type="button"
                class="routing-segment-btn"
                :class="{ active: effectiveRoutingPolicy(selectedUnit) === 'path' }"
                @click="setRoutingPolicy(selectedUnit, 'path')"
              >Path</button>
              <button
                type="button"
                class="routing-segment-btn"
                :class="{ active: selectedUnit.routing_explicit === 'direct' }"
                @click="setRoutingPolicy(selectedUnit, 'direct')"
              >Direct</button>
              <button
                type="button"
                class="routing-segment-btn routing-segment-danger"
                :class="{ active: selectedUnit.routing_explicit === 'flood' }"
                @click="setRoutingPolicy(selectedUnit, 'flood')"
              >Flood</button>
            </div>
            <p v-if="selectedUnit.routing_explicit === 'flood'" class="routing-policy-warning">
              Always flood. High airtime on every send.
            </p>
            <p class="routing-policy-hint">Book policy: {{ routingPolicyLabel(selectedUnit) }}</p>
          </section>
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
                :class="'health-' + healthHeadline(selectedUnit, fleet.now)"
              >
                {{ healthMark(selectedUnit, fleet.now) }}
              </span>
            </div>
            <ul v-if="hasHealthIssues(selectedUnit.health)" class="health-issues">
              <li
                v-for="(issue, i) in selectedUnit.health.issues"
                :key="i"
                :class="'health-' + issue.status"
              >
                <div class="health-issue-head">
                  <span>{{ issue.name }}: {{ issue.reason || issue.status }}</span>
                  <button
                    v-if="issue.name === 'Stability'"
                    type="button"
                    class="health-dismiss"
                    @click="ackStability(selectedUnit, $event)"
                  >
                    Dismiss
                  </button>
                </div>
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
          <section v-if="selectedUnit.prefs?.length">
            <h3>Radio prefs</h3>
            <dl>
              <template v-for="pref in selectedUnit.prefs" :key="pref.id">
                <dt>{{ pref.label }}</dt>
                <dd>
                  {{ pref.value }}
                  <span class="dim">{{ pref.state === 'due' ? 'due' : 'applied' }}</span>
                </dd>
              </template>
            </dl>
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
                  title="MOTA bootloader can apply firmware updates"
                >MOTA</span>
                <span
                  v-else-if="selectedUnit.ota?.running?.bl_apply === false"
                  class="ota-tag ota-tag-warn"
                  title="Stock bootloader cannot apply firmware updates"
                >no MOTA</span>
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
          </div>
        </div>
    </Teleport>
    <div
      v-if="consoleOpen"
      class="console-modal overlay-modal"
      @mousedown.self="hideConsoleAtEvent"
    >
          <div class="console-shell" role="dialog" aria-modal="true" aria-label="Console" @mousedown.stop>
            <div class="console-tabs">
              <button
                v-for="tab in consoleTabs"
                :key="tab.tab_id"
                type="button"
                class="console-tab"
                :class="{ on: tab.tab_id === consoleActiveId, busy: tab.state === 'sending', failed: tab.state === 'failed' }"
                @click="selectConsoleTab(tab.tab_id)"
              >
                <span>{{ consoleTabLabel(tab) }}</span>
                <span v-if="tab.unread" class="console-tab-unread" aria-label="Unread">*</span>
                <span v-if="tab.pending.length" class="console-tab-count">{{ tab.pending.length }}</span>
                <span class="console-tab-close" title="Close tab" @click.stop="closeConsoleTab(tab.tab_id)">×</span>
              </button>
              <div class="console-tab-add-wrap">
                <button type="button" class="console-tab-add" title="New tab" @click="toggleConsolePicker">+</button>
                <div v-if="consolePickerOpen" class="console-picker">
                  <input
                    ref="consolePickerSearch"
                    v-model="consolePickerQuery"
                    class="console-picker-search"
                    type="search"
                    placeholder="Open unit…"
                    autocomplete="off"
                    spellcheck="false"
                  />
                  <button
                    v-for="unit in consolePickerUnits"
                    :key="unit.key"
                    type="button"
                    class="console-picker-item"
                    @click="addConsoleTab(unit.key)"
                  >
                    {{ unitTitle(unit) }}
                    <span class="console-picker-id">{{ unit.unit_id || unit.key }}</span>
                  </button>
                  <p v-if="!consolePickerUnits.length" class="console-picker-empty">No units</p>
                </div>
              </div>
              <button type="button" class="console-modal-close" aria-label="Hide console" @click="hideConsole">×</button>
            </div>
            <section v-if="consoleActive" class="console-panel">
              <div class="console-toolbar">
                <button
                  type="button"
                  class="console-copy"
                  :class="{ copied: consoleCopied }"
                  :disabled="!consoleCanCopy"
                  :title="consoleCopied ? 'Copied' : 'Copy history'"
                  :aria-label="consoleCopied ? 'Copied' : 'Copy history'"
                  @click="copyConsoleHistory"
                >
                  <svg v-if="!consoleCopied" viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6">
                    <rect x="7" y="5.5" width="9" height="11.5" rx="1.4" />
                    <path d="M13 5.5V4.4A1.4 1.4 0 0 0 11.6 3H4.4A1.4 1.4 0 0 0 3 4.4v10.2A1.4 1.4 0 0 0 4.4 16H6" />
                  </svg>
                  <svg v-else viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M4.5 10.5l3.4 3.4 7.6-7.8" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  <span v-if="consoleCopied">Copied</span>
                </button>
                <button
                  type="button"
                  class="console-clear"
                  :disabled="!consoleActive.history.length"
                  @click="clearConsoleHistory"
                >
                  Clear history
                </button>
              </div>
              <div ref="consoleScroll" class="console-transcript">
                <div v-for="(row, i) in consoleActive.history" :key="i" class="console-block">
                  <div class="console-cmd">&gt; {{ row.cmd }}</div>
                  <pre v-if="row.reply" class="console-reply">{{ row.reply }}</pre>
                  <div v-if="row.error" class="console-status">
                    <pre class="console-error">{{ row.error }}</pre>
                    <button
                      v-if="consoleHistoryRetry(row)"
                      type="button"
                      class="console-cancel"
                      @click="retryConsoleCmd(row.cmd)"
                    >
                      Retry
                    </button>
                  </div>
                </div>
                <div v-if="consoleBusy" class="console-block">
                  <div class="console-cmd">&gt; {{ consoleActive.cmd }}</div>
                  <div class="console-status">
                    {{ consoleStatusLine }}
                    <button type="button" class="console-cancel" @click="cancelConsoleSend">Cancel</button>
                  </div>
                </div>
                <div v-else-if="consoleFailed" class="console-block">
                  <div class="console-cmd">&gt; {{ consoleActive.cmd }}</div>
                  <div class="console-status">
                    <pre v-if="consoleActive.error" class="console-error">{{ consoleActive.error }}</pre>
                    <button type="button" class="console-cancel" @click="retryConsoleSend">Retry</button>
                    <button
                      v-if="consoleActive.pending.length"
                      type="button"
                      class="console-cancel"
                      @click="skipConsoleFailed"
                    >
                      Continue
                    </button>
                  </div>
                </div>
              </div>
              <div v-if="consoleActive.pending.length" class="console-staging">
                <div class="console-staging-head">
                  <span>Queued</span>
                  <button type="button" class="console-clear" @click="clearPendingAll">Clear queued</button>
                </div>
                <div v-for="item in consoleActive.pending" :key="item.id" class="console-stage-row">
                  <input
                    v-model="item.cmd"
                    class="console-stage-input"
                    type="text"
                    spellcheck="false"
                    autocomplete="off"
                    @blur="savePendingCmd(consoleActive, item)"
                    @keydown.enter.prevent="savePendingCmd(consoleActive, item)"
                  />
                  <button type="button" class="console-tab-close" title="Remove" @click="dropPendingCmd(consoleActive, item)">×</button>
                </div>
              </div>
              <form class="console-input-row" @submit.prevent="submitConsoleLine">
                <input
                  v-model="consoleActive.draft"
                  class="console-input"
                  type="text"
                  placeholder="ota stats"
                  autocomplete="off"
                  spellcheck="false"
                  @keydown.up.prevent="recallConsoleCmd(-1)"
                  @keydown.down.prevent="recallConsoleCmd(1)"
                  @input="onConsoleDraftInput"
                />
                <button type="submit" class="manual-btn" :disabled="!consoleActive.draft.trim()">Send</button>
              </form>
            </section>
            <p v-else class="console-empty">Pick a unit to open a tab.</p>
          </div>
        </div>
    <div
      v-show="mapOpen"
      class="map-modal overlay-modal"
      @mousedown.self="hideMapAtEvent"
    >
      <div class="map-shell" role="dialog" aria-modal="true" aria-label="Map" @mousedown.stop>
        <div class="map-modal-head">
          <span class="map-modal-title">Map</span>
          <button type="button" class="console-modal-close" aria-label="Hide map" @click="hideMap">×</button>
        </div>
        <div id="layout" class="map-layout" :class="{ 'detail-open': !!selectedUnit }">
          <div id="map-wrap">
            <div id="map"></div>
          </div>
          <aside id="map-detail-slot" class="map-detail-slot" :class="{ 'is-open': !!selectedUnit }"></aside>
        </div>
      </div>
    </div>
  `,
};

createApp(App).mount("#app");
