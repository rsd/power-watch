#!/usr/bin/env node

// Exercises omarchy-plugin/Model.js, which is written as a QML JS module (bare
// function declarations, no exports) and so is loaded here by evaluating it and
// lifting the names out — the same approach as omavnc's tests/model-test.js.
//
// Times are local; the suite pins TZ before the first Date is built so the
// axis labels are deterministic.

process.env.TZ = "UTC"

const fs = require("fs")
const path = require("path")
const assert = require("assert")

const pluginDir = path.join(__dirname, "..", "..", "omarchy-plugin")
const source = fs.readFileSync(path.join(pluginDir, "Model.js"), "utf8")
const NAMES = [
  "APPROACH_FRACTION", "STALE_INTERVALS", "DEFAULT_INTERVAL_S", "DEFAULT_THRESHOLD_W",
  "STALE_CHECK_MS", "HISTORY_THROTTLE_MS", "GAP_STEPS",
  "CURRENT_MAX_BYTES", "HISTORY_MAX_BYTES",
  "CURRENT_MAX_CHARS", "HISTORY_PARSE_CHARS", "HISTORY_MAX_LINES", "HISTORY_MAX_LINE_CHARS",
  "MAX_RENDER_POINTS", "MAX_DASH_SEGMENTS", "RANGES", "SERVICE_HINT", "CHART_STYLE",
  "BREAKDOWN_ROWS_PER_COLUMN", "TOTAL_SOURCES", "GPU_STATUSES", "MAX_GPU_DEVICES", "LOWER_BOUND_MARK",
  "MAX_GPU_DEVICE_SCAN", "BAR_WIDEST_TEXT", "BAR_VERTICAL_SLOTS", "PSYS_LABEL", "PSYS_BAND_LABEL",
  "CURRENT_READ_MIN_MS",
  "oneOf", "pciAddress", "parseGpuDevices", "isLowerBound", "boundText", "currentStatText",
  "totalLabel", "totalRowValue", "gpuRowValue", "gpuCountText", "gpuListCut", "showsMeasuredAc",
  "finiteIn", "safeLabel", "canvasFont", "rangeFor", "stepFor", "stateDir", "fileUrl", "historyPath",
  "parseCurrent", "isStale", "shouldRelist", "gateDecision", "loadFailure", "level", "levelText",
  "statusLine", "restSourceLabel", "sampleTitle", "sampleNotes",
  "formatNumber", "formatWatts", "formatAmps", "formatPercent", "barText", "barLines",
  "powerSourceLabel", "kindLabel", "staleMessage", "tooltipText", "breakdown", "capStatus",
  "normalizePoint", "parseHistory", "filterRange", "endOf", "splitGaps", "pointMode", "modeSpans",
  "stats", "mergePoints", "consolidate", "niceAxis", "timeTicks", "chartModel", "chartLayout",
  "clampLabelX", "dashSegments"
]
const model = new Function(source + "\nreturn {" + NAMES.join(",") + "}")()

const tests = []
const test = (name, fn) => tests.push([name, fn])

// What the service writes: the --once --json report (with the generic-config
// keys) plus the four extra keys.
const sample = (over) => Object.assign({
  time: "2026-10-24T10:46:08-03:00", ac_online: true, battery_status: "Not charging",
  battery_capacity: 63, battery_w: 0.0, cpu_w: 7.78, gpu_w: 15.01, gpu_limit_w: 190.0,
  gpu_pstate: "P5", rest_w: 29.77, rest_source: "calibrate", charge_w: 0.0, total_w: 52.56,
  total_kind: "estimated", incomplete: false, adapter_v: 19.5, adapter_current_a: 2.7,
  adapter_rating_w: 330.0, threshold_w: 150.0, errors: [],
  total_source: "model", total_lower_bound: false, psys_w: null, gpu_status: "active",
  gpu_devices: [{ pci: "0000:01:00.0", vendor: "nvidia", status: "active", power_w: 15.01 }],
  interval_s: 2.0, cpu_limit_w: 91.0, gpu_max_limit_w: null, epoch: 1761300000.0
}, over || {})
const parsed = (over) => model.parseCurrent(JSON.stringify(sample(over))).data
// No string the UI shows may carry a JS artefact or a non-finite number.
const assertClean = (text, what) =>
  assert.ok(!/NaN|null|undefined|Infinity|\[object/.test(text), (what || "text") + ": " + JSON.stringify(text))

const point = (t, over) => Object.assign(
  { t, n: 1, avg: 80, max: 90, cpu: 8, gpu: 38, rest: 25, chg: 0, ac: 1, k: "e" }, over || {})
const jsonl = (points) => points.map(p => JSON.stringify(p)).join("\n") + "\n"

// ---------------------------------------------------------------- current

test("parses a full current.json", () => {
  const r = model.parseCurrent(JSON.stringify(sample()))
  assert.strictEqual(r.ok, true)
  assert.strictEqual(r.data.totalW, 52.56)
  assert.strictEqual(r.data.intervalS, 2)
  assert.strictEqual(r.data.cpuLimitW, 91)
  assert.strictEqual(r.data.gpuMaxLimitW, null)
  assert.strictEqual(r.data.totalKind, "estimated")
  assert.strictEqual(r.data.restSource, "calibrate")
  assert.strictEqual(r.data.epoch, 1761300000)
})

test("missing and unreadable current.json are distinguishable", () => {
  assert.deepStrictEqual(model.parseCurrent(""), { ok: false, error: "missing" })
  assert.deepStrictEqual(model.parseCurrent(undefined), { ok: false, error: "missing" })
  assert.deepStrictEqual(model.parseCurrent('{"total_w": 5'), { ok: false, error: "unreadable" })
  assert.deepStrictEqual(model.parseCurrent("[1,2]"), { ok: false, error: "unreadable" })
  assert.deepStrictEqual(model.parseCurrent("null"), { ok: false, error: "unreadable" })
})

test("an oversized current.json is not parsed", () => {
  const big = JSON.stringify(sample({ pad: "x".repeat(model.CURRENT_MAX_CHARS) }))
  assert.deepStrictEqual(model.parseCurrent(big), { ok: false, error: "too large" })
})

test("numbers are bound: wrong types, NaN-ish and absurd values become null", () => {
  const r = model.parseCurrent(JSON.stringify(sample({
    total_w: "82", cpu_w: 1e9, gpu_w: null, adapter_current_a: true, interval_s: -1,
    threshold_w: 0, epoch: "now", battery_capacity: 250
  })))
  assert.strictEqual(r.data.totalW, null)
  assert.strictEqual(r.data.cpuW, null)
  assert.strictEqual(r.data.gpuW, null)
  assert.strictEqual(r.data.adapterA, null)
  assert.strictEqual(r.data.intervalS, model.DEFAULT_INTERVAL_S)
  assert.strictEqual(r.data.thresholdW, model.DEFAULT_THRESHOLD_W)
  assert.strictEqual(r.data.epoch, null)
  assert.strictEqual(r.data.batteryCapacity, null)
})

test("file-sourced strings are stripped of markup and capped", () => {
  const r = model.parseCurrent(JSON.stringify(sample({
    rest_source: "<img src='http://x/y'>&calibrate\n", battery_status: "A".repeat(500), total_kind: "<b>"
  })))
  assert.ok(!/[<>&\n]/.test(r.data.restSource), r.data.restSource)
  assert.ok(r.data.restSource.length <= 24)
  assert.strictEqual(r.data.batteryStatus.length, 24)
  assert.strictEqual(r.data.totalKind, "")
  assert.strictEqual(model.safeLabel(42), "")
})

test("safeLabel strips bidi overrides, isolates, zero-width and C1 characters", () => {
  const hostile = "Not\u202e gnigrahc\u2066x\u2069\u200b\u200f\ufeff\u0085\u2028end"
  const out = model.safeLabel(hostile, 40)
  assert.ok(!/[\u0080-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/.test(out), JSON.stringify(out))
  assert.strictEqual(out, "Not gnigrahcxend")
  assert.strictEqual(model.parseCurrent(JSON.stringify(sample({ battery_status: "\u202eChar" }))).data.batteryStatus, "Char")
})

test("canvas font string survives a family name with quotes or backslashes", () => {
  assert.strictEqual(model.canvasFont(11, "JetBrainsMono Nerd Font"), '11px "JetBrainsMono Nerd Font"')
  assert.strictEqual(model.canvasFont(11, 'Evil" Font\\'), '11px "Evil Font"')
  assert.strictEqual(model.canvasFont(11, '"'), "11px sans-serif")
  assert.strictEqual(model.canvasFont(NaN, null), "10px sans-serif")
  // exactly one pair of quotes, always balanced
  assert.strictEqual((model.canvasFont(12, 'a"b"c').match(/"/g) || []).length, 2)
})

test("current.json reads are throttled to one a second; staleness allows for it", () => {
  assert.ok(model.CURRENT_READ_MIN_MS >= 1000, "at most one current.json read per second per bar")
  // A sub-second interval counts as one second: a sample read once a second
  // must not flash stale between reads.
  const fast = parsed({ interval_s: 0.05 })
  assert.strictEqual(model.isStale(fast, 1761300000 + 2.9), false)
  assert.strictEqual(model.isStale(fast, 1761300000 + 3.1), true)
  // At or above a second the rule is unchanged: 3 intervals.
  assert.strictEqual(model.isStale(parsed({ interval_s: 1 }), 1761300000 + 3.1), true)
  const bar = fs.readFileSync(path.join(pluginDir, "BarWidget.qml"), "utf8").replace(/\/\/.*$/gm, "")
  assert.ok(/id:\s*currentGate[\s\S]*?minIntervalMs:\s*Model\.CURRENT_READ_MIN_MS/.test(bar),
    "the current.json reader is throttled")
})

test("staleness: missing, no epoch, older than 3 intervals, far future", () => {
  const cur = model.parseCurrent(JSON.stringify(sample())).data
  assert.strictEqual(model.isStale(null, 1761300000), true)
  assert.strictEqual(model.isStale(Object.assign({}, cur, { epoch: null }), 1761300000), true)
  assert.strictEqual(model.isStale(cur, 1761300000 + 6), false)
  assert.strictEqual(model.isStale(cur, 1761300000 + 6.5), true)
  assert.strictEqual(model.isStale(cur, 1761300000 - 60), true)
  const slow = Object.assign({}, cur, { intervalS: 10 })
  assert.strictEqual(model.isStale(slow, 1761300000 + 29), false)
  assert.strictEqual(model.STALE_INTERVALS, 3)
})

test("the staleness tick re-lists only while current.json is absent, never a present file", () => {
  assert.strictEqual(model.shouldRelist("missing"), true)
  // An oversized or unreadable file is watched already; re-reading it on a
  // timer is what cost 0.8-1.7 GB per read in the audit.
  for (const outcome of ["too large", "unreadable", "ok", "pending", ""])
    assert.strictEqual(model.shouldRelist(outcome), false, outcome)
  assert.ok(model.STALE_CHECK_MS >= 10000, "staleness check must be >= 10 s")
  assert.ok(model.HISTORY_THROTTLE_MS >= 5000, "history repaint throttle must be >= 5 s")
})

test("levels: normal, approaching at 85% of threshold, over at threshold", () => {
  assert.strictEqual(model.APPROACH_FRACTION, 0.85)
  assert.strictEqual(model.level(100, 150), "normal")
  assert.strictEqual(model.level(127.4, 150), "normal")
  assert.strictEqual(model.level(127.5, 150), "approaching")
  assert.strictEqual(model.level(149.9, 150), "approaching")
  assert.strictEqual(model.level(150, 150), "over")
  assert.strictEqual(model.level(300, 150), "over")
  assert.strictEqual(model.level(null, 150), "unknown")
  assert.strictEqual(model.level(130, 0), "approaching")
})

test("read gate: size checked against the cap before any read, at the boundary", () => {
  const cap = model.CURRENT_MAX_BYTES
  assert.deepStrictEqual(model.gateDecision(null, cap, ""), { action: "missing" })
  assert.deepStrictEqual(model.gateDecision({ size: cap + 1, modified: 1 }, cap, ""), { action: "too large" })
  assert.deepStrictEqual(model.gateDecision({ size: 209715200, modified: 1 }, cap, ""), { action: "too large" })
  assert.deepStrictEqual(model.gateDecision({ size: cap, modified: 5 }, cap, ""), { action: "read", version: cap + ":5" })
  // A 0-byte entry is never read: /proc files list as 0 and read as megabytes.
  assert.deepStrictEqual(model.gateDecision({ size: 0, modified: 5 }, cap, ""), { action: "missing" })
  assert.deepStrictEqual(model.gateDecision({ size: 0, modified: 5 }, model.HISTORY_MAX_BYTES, "0:5"), { action: "missing" },
    "not even a version remembered from before")
  assert.deepStrictEqual(model.gateDecision({ size: 1, modified: 5 }, cap, ""), { action: "read", version: "1:5" })
  assert.deepStrictEqual(model.gateDecision({ size: 600, modified: 5 }, cap, "600:5"), { action: "none" })
  assert.strictEqual(model.gateDecision({ size: 600, modified: 6 }, cap, "600:5").action, "read")
  assert.strictEqual(model.gateDecision({ size: 601, modified: 5 }, cap, "600:5").action, "read")
  assert.deepStrictEqual(model.gateDecision({ size: 10, isDir: true }, cap, ""), { action: "unreadable" })
  assert.deepStrictEqual(model.gateDecision({ size: NaN }, cap, ""), { action: "unreadable" })
  assert.deepStrictEqual(model.gateDecision({ size: -1 }, cap, ""), { action: "unreadable" })
  assert.deepStrictEqual(model.gateDecision({ size: 1 }, 0, ""), { action: "too large" }, "no cap = no read")
  assert.ok(model.HISTORY_MAX_BYTES >= 12 * 1048576, "the hard ceiling holds a raw tier at the 0.05 s floor")
  assert.ok(model.CURRENT_MAX_BYTES <= model.CURRENT_MAX_CHARS, "UTF-8 bytes >= chars: the gate implies the parse cap")
})

test("load failures: not found is missing, anything else is unreadable", () => {
  assert.strictEqual(model.loadFailure(true), "missing")
  assert.strictEqual(model.loadFailure(false), "unreadable")
  const cur = model.parseCurrent(JSON.stringify(sample())).data
  assert.notStrictEqual(model.staleMessage(null, "unreadable"), model.staleMessage(null, "missing"))
  assert.notStrictEqual(model.staleMessage(cur, "too large"), model.staleMessage(cur, ""))
  assert.ok(model.staleMessage(null, "too large").includes("16 KiB"))
})

// ------------------------------------------------------------- formatting

test("watts, amps and percent formatting", () => {
  assert.strictEqual(model.formatWatts(82.4), "82 W")
  assert.strictEqual(model.formatWatts(82.5), "83 W")
  assert.strictEqual(model.formatWatts(null), "-- W")
  assert.strictEqual(model.formatWatts(-0.2), "0 W")
  assert.strictEqual(model.formatWatts(7.78, 1), "7.8 W")
  assert.strictEqual(model.formatAmps(4.21), "4.2 A")
  assert.strictEqual(model.formatAmps(undefined), "-- A")
  assert.strictEqual(model.formatPercent(82.5, 330), "25%")
  assert.strictEqual(model.formatPercent(82.5, null), "--")
  assert.strictEqual(model.formatPercent(82.5, 0), "--")
})

test("bar text, vertical lines and stale bar", () => {
  const cur = model.parseCurrent(JSON.stringify(sample({ total_w: 81.6 }))).data
  assert.strictEqual(model.barText(cur, false), "82 W")
  assert.strictEqual(model.barText(cur, true), "-- W")
  assert.strictEqual(model.barText(null, false), "-- W")
  assert.deepStrictEqual(model.barLines(cur, false), ["82", "W"])
  assert.deepStrictEqual(model.barLines(null, true), ["--", "W"])
})

test("the level is stated in words, not only in colour", () => {
  assert.strictEqual(model.levelText("over", 150), "Over the 150 W threshold")
  assert.strictEqual(model.levelText("approaching", 150), "Approaching the 150 W threshold (85%+)")
  assert.strictEqual(model.levelText("normal", 150), "Below the 150 W threshold")
  assert.strictEqual(model.levelText("unknown", 150), "")
  const over = model.parseCurrent(JSON.stringify(sample({ total_w: 160 }))).data
  assert.ok(model.tooltipText(over, false).includes("Over the 150 W threshold"))
  assert.strictEqual(model.statusLine(over), "Estimated · AC · Over the 150 W threshold")
  const near = model.parseCurrent(JSON.stringify(sample({ total_w: 130 }))).data
  assert.ok(model.tooltipText(near, false).includes("Approaching"))
})

test("the breakdown title says when the sample is not current", () => {
  const cur = model.parseCurrent(JSON.stringify(sample())).data
  assert.strictEqual(model.sampleTitle(cur, false), "NOW")
  assert.strictEqual(model.sampleTitle(cur, true), "LAST SAMPLE")
  assert.strictEqual(model.sampleTitle(null, true), "NO SAMPLE")
})

test("tooltip: amps, kind, source; stale tooltip names the service", () => {
  const cur = model.parseCurrent(JSON.stringify(sample({ total_w: 82, adapter_current_a: 4.2 }))).data
  const tip = model.tooltipText(cur, false)
  assert.ok(tip.includes("82 W") && tip.includes("4.2 A"), tip)
  assert.ok(tip.includes("Estimated") && tip.includes("AC"), tip)
  const bat = model.parseCurrent(JSON.stringify(sample({ ac_online: false, total_kind: "measured" }))).data
  assert.ok(model.tooltipText(bat, false).includes("Measured · Battery"))
  const stale = model.tooltipText(cur, true)
  assert.ok(stale.includes("not running"), stale)
  assert.ok(stale.includes("systemctl --user enable --now power-watch.service"), stale)
  assert.ok(model.tooltipText(null, true).includes("no current.json"))
  assert.ok(model.tooltipText(null, true, "unreadable").includes("current.json is unreadable"))
  assert.ok(model.tooltipText(null, true, "too large").includes("current.json is too large"))
  assert.ok(model.staleMessage(cur, "").includes("last sample is old"))
})

test("breakdown rows cover CPU, GPU, rest+source, charging, amps, rating, kind, source", () => {
  const cur = model.parseCurrent(JSON.stringify(sample({ charge_w: 12.3, total_w: 165 }))).data
  const rows = model.breakdown(cur)
  const get = label => rows.find(r => r.label === label).value
  assert.strictEqual(get("CPU"), "8 W")
  assert.strictEqual(get("GPU"), "15 W (P5)")
  assert.strictEqual(get("Rest"), "30 W (calibrate calibration)")
  assert.strictEqual(get("Charging"), "12 W")
  assert.strictEqual(get("Battery"), "Not charging, 63%")
  assert.strictEqual(get("Adapter current"), "2.7 A")
  assert.strictEqual(get("Of adapter rating"), "50% of 330 W")
  assert.strictEqual(get("Total is"), "Estimated")
  assert.strictEqual(get("Power source"), "AC")
  assert.strictEqual(model.breakdown(null).length, rows.length)
  // Two columns top to bottom: the load terms, then the supply side.
  const col = model.BREAKDOWN_ROWS_PER_COLUMN
  assert.deepStrictEqual(rows.slice(0, col).map(r => r.label), ["CPU", "GPU", "Rest", "Charging", "Total is"])
  assert.deepStrictEqual(rows.slice(col).map(r => r.label), ["Power source", "Battery", "Adapter current", "Of adapter rating"])
})

test("wording: rest source as the CLI prints it, sentence-case unknowns", () => {
  assert.strictEqual(model.restSourceLabel("calibrate"), "calibrate calibration")
  assert.strictEqual(model.restSourceLabel("passive"), "passive calibration")
  assert.strictEqual(model.restSourceLabel("default"), "DEFAULT — run power-watch calibrate")
  assert.strictEqual(model.restSourceLabel(""), "")
  const none = model.breakdown(null)
  const get = label => none.find(r => r.label === label).value
  assert.strictEqual(get("Charging"), "No")
  assert.strictEqual(get("Battery"), "Unknown")
  assert.strictEqual(get("Power source"), "Unknown")
  for (const row of none) assert.ok(/^[A-Z0-9-]/.test(row.value), row.label + ": " + row.value)
  const def = model.parseCurrent(JSON.stringify(sample({ rest_source: "default", incomplete: true }))).data
  const notes = model.sampleNotes(def)
  assert.ok(notes.includes("DEFAULT — run power-watch calibrate"), notes)
  assert.ok(notes.includes("unknown terms counted as 0 W"), notes)
  assert.strictEqual(model.sampleNotes(model.parseCurrent(JSON.stringify(sample())).data), "")
})

test("cap status: PL1, GPU against max, no invented max, unknowns", () => {
  const base = model.parseCurrent(JSON.stringify(sample({ gpu_max_limit_w: 190 }))).data
  assert.deepStrictEqual(model.capStatus(base), { cpu: "PL1 91 W", gpu: "190 W (max, uncapped)", gpuCapped: false })
  const capped = model.parseCurrent(JSON.stringify(sample({ gpu_limit_w: 80, cpu_limit_w: 45, gpu_max_limit_w: 190 }))).data
  assert.deepStrictEqual(model.capStatus(capped), { cpu: "PL1 45 W", gpu: "80 W of 190 W (capped)", gpuCapped: true })
  // No maximum reported: the limit as is, never measured against a made-up one.
  const noMax = model.parseCurrent(JSON.stringify(sample({ gpu_limit_w: 80 }))).data
  assert.deepStrictEqual(model.capStatus(noMax), { cpu: "PL1 91 W", gpu: "80 W (max unknown)", gpuCapped: false })
  const withMax = model.parseCurrent(JSON.stringify(sample({ gpu_limit_w: 150, gpu_max_limit_w: 200 }))).data
  assert.strictEqual(model.capStatus(withMax).gpu, "150 W of 200 W (capped)")
  const none = model.parseCurrent(JSON.stringify(sample({ gpu_limit_w: null, cpu_limit_w: null }))).data
  assert.deepStrictEqual(model.capStatus(none), { cpu: "Unknown", gpu: "Unknown", gpuCapped: false })
})

// ------------------------------------------------ generic-config report keys

test("parses the new report keys", () => {
  const c = parsed()
  assert.strictEqual(c.totalSource, "model")
  assert.strictEqual(c.totalLowerBound, false)
  assert.strictEqual(c.psysW, null)
  assert.strictEqual(c.gpuStatus, "active")
  assert.deepStrictEqual(c.gpuDevices, [{ pci: "0000:01:00.0", vendor: "nvidia", status: "active", powerW: 15.01 }])
  const p = parsed({ total_source: "psys", psys_w: 61.4, total_kind: "measured" })
  assert.strictEqual(p.totalSource, "psys")
  assert.strictEqual(p.psysW, 61.4)
})

test("malformed new keys fall back to not-reported, never to a value", () => {
  const c = parsed({
    total_source: "PSYS", total_lower_bound: "true", psys_w: "61", gpu_status: "<b>active</b>",
    gpu_devices: "0000:01:00.0"
  })
  assert.strictEqual(c.totalSource, "")
  assert.strictEqual(c.totalLowerBound, false)
  assert.strictEqual(c.psysW, null)
  assert.strictEqual(c.gpuStatus, "")
  assert.deepStrictEqual(c.gpuDevices, [])
  assert.strictEqual(parsed({ psys_w: 1e9 }).psysW, null)
  assert.strictEqual(parsed({ total_lower_bound: 1 }).totalLowerBound, false)
  // A lower bound needs a number to bound.
  assert.strictEqual(parsed({ total_lower_bound: true, total_w: null }).totalLowerBound, false)
  // Keys missing altogether (an older service) read the same way.
  const old = sample()
  for (const k of ["total_source", "total_lower_bound", "psys_w", "gpu_status", "gpu_devices"]) delete old[k]
  const o = model.parseCurrent(JSON.stringify(old)).data
  assert.deepStrictEqual([o.totalSource, o.totalLowerBound, o.psysW, o.gpuStatus, o.gpuDevices], ["", false, null, "", []])
  assert.strictEqual(model.gpuRowValue(o), "15 W (P5)", "no gpu_status: watts, as before")
})

test("gpu_devices: capped, non-objects dropped, each field sanitised", () => {
  const many = Array.from({ length: 100 }, () => ({ pci: "0000:01:00.0", vendor: "nvidia", status: "active", power_w: 1 }))
  const m = parsed({ gpu_devices: many })
  assert.strictEqual(m.gpuDevices.length, model.MAX_GPU_DEVICES)
  assert.strictEqual(m.gpuDeviceTotal, 100, "the rest are counted")
  assert.strictEqual(m.gpuDevicesPartial, false)
  const d = parsed({ gpu_devices: [
    null, 7, "x", [1], { pci: "01:00.0<script>", vendor: "intel", status: "on", power_w: "5" },
    { pci: "0000:03:00.0", vendor: "amd", status: "suspended", power_w: null, extra: "y" }
  ] }).gpuDevices
  assert.deepStrictEqual(d, [
    { pci: "", vendor: "", status: "", powerW: null },
    { pci: "0000:03:00.0", vendor: "amd", status: "suspended", powerW: null }
  ])
  assert.strictEqual(model.pciAddress("0000:01:00.0‮"), "")
  assert.strictEqual(model.pciAddress("0000:0G:00.0"), "")
})

test("gpu_devices: the scan has its own cap, whatever the caller bounded", () => {
  const cap = model.MAX_GPU_DEVICE_SCAN
  assert.ok(cap >= model.MAX_GPU_DEVICES && cap <= 1024, String(cap))
  const huge = new Array(1e6).fill(0)
  const t0 = Date.now()
  const r = model.parseGpuDevices(huge)
  assert.ok(Date.now() - t0 < 100, "took " + (Date.now() - t0) + " ms")
  assert.deepStrictEqual(r, { devices: [], total: 0, partial: true })
  // An entry past the cap is never looked at.
  const late = new Array(cap).fill(null).concat([{ status: "active" }])
  assert.deepStrictEqual(model.parseGpuDevices(late), { devices: [], total: 0, partial: true })
  const exact = new Array(cap - 1).fill(null).concat([{ status: "active" }])
  assert.strictEqual(model.parseGpuDevices(exact).devices.length, 1)
  assert.strictEqual(model.parseGpuDevices(exact).partial, false)
})

test("more than MAX_GPU_DEVICES GPUs: the row says the list was cut", () => {
  const dev = (status) => ({ pci: "0000:01:00.0", vendor: "nvidia", status: status, power_w: 1 })
  const row = over => model.gpuRowValue(parsed(over))
  const twenty = Array.from({ length: 20 }, () => dev("active"))
  assert.strictEqual(row({ gpu_devices: twenty, gpu_w: 20 }), "20 W (16 of 20 GPUs listed)")
  assert.strictEqual(row({ gpu_devices: twenty.map(() => dev("suspended")), gpu_status: "suspended" }),
    "Suspended (16 of 20 GPUs listed)")
  // Exactly the cap: nothing was cut.
  const sixteen = twenty.slice(0, 16)
  assert.strictEqual(row({ gpu_devices: sixteen, gpu_w: 16 }), "16 W (16 GPUs)")
  assert.strictEqual(model.gpuListCut(parsed({ gpu_devices: sixteen })), false)
  // Past the scan the count is a floor.
  // Past the scan the count is a floor (built from the parse directly: that
  // many entries do not fit through the current.json caps).
  const flood = model.parseGpuDevices(Array.from({ length: model.MAX_GPU_DEVICE_SCAN + 5 }, () => dev("active")))
  const c = Object.assign(parsed({ gpu_w: 5 }), { gpuDevices: flood.devices, gpuDeviceTotal: flood.total,
    gpuDevicesPartial: flood.partial })
  assert.strictEqual(model.gpuRowValue(c), "5 W (16 of " + model.MAX_GPU_DEVICE_SCAN + "+ GPUs listed)")
  assert.strictEqual(model.gpuCountText(parsed()), "", "one GPU: no count")
})

test("a lower-bound total is marked in the bar, the hero, the tooltip and the stats", () => {
  const lb = parsed({ total_w: 81.6, incomplete: true, total_lower_bound: true, gpu_w: null, gpu_status: "unknown",
    adapter_current_a: 4.18 })
  const mark = model.LOWER_BOUND_MARK
  assert.strictEqual(mark, "≥")
  assert.strictEqual(model.barText(lb, false), "≥82 W")
  assert.strictEqual(model.barText(lb, true), "-- W", "a stale lower bound is no number at all")
  assert.deepStrictEqual(model.barLines(lb, false), ["≥", "82", "W"], "the mark has its own line")
  // The bar always reserves room for the mark, so it never resizes when the
  // mark toggles: the widest text has it, the vertical slots fit its line.
  assert.strictEqual(model.BAR_WIDEST_TEXT, "≥000 W")
  assert.strictEqual(model.barLines(lb, false).length, model.BAR_VERTICAL_SLOTS)
  for (const c of [lb, parsed(), parsed({ total_w: 999.4 }), null])
    for (const stale of [false, true]) {
      assert.ok(model.barLines(c, stale).length <= model.BAR_VERTICAL_SLOTS)
      assert.ok(model.barText(c, stale).length <= model.BAR_WIDEST_TEXT.length, model.barText(c, stale))
    }
  const tip = model.tooltipText(lb, false)
  assert.ok(tip.startsWith("Adapter ≥82 W · ≥4.2 A"), tip)
  assert.ok(tip.includes("Estimated (lower bound) · AC"), tip)
  assert.ok(!tip.includes("incomplete"), tip)
  assert.strictEqual(model.statusLine(lb), "Estimated (lower bound) · AC · Below the 150 W threshold")
  const stats = model.stats([], lb, false)
  assert.strictEqual(model.currentStatText(stats, lb, false), "≥82 W")
  assert.strictEqual(model.currentStatText(model.stats([], lb, true), lb, true), "-- W")
  assert.strictEqual(model.currentStatText(model.stats([], parsed(), false), parsed(), false), "53 W")
  const get = label => model.breakdown(lb).find(r => r.label === label).value
  assert.strictEqual(get("Total is"), "Estimated (lower bound)")
  assert.strictEqual(get("Adapter current"), "≥4.2 A")
  assert.strictEqual(get("Of adapter rating"), "≥25% of 330 W")
  assert.strictEqual(get("GPU"), "Unknown")
  assert.strictEqual(model.sampleNotes(lb), "Lower bound: unknown terms counted as 0 W.")
})

test("incomplete without a lower bound keeps the incomplete wording", () => {
  // Battery side: the measured total itself is unknown.
  const bat = parsed({ ac_online: false, total_kind: "measured", total_source: "battery", total_w: null,
    incomplete: true, adapter_current_a: null })
  assert.strictEqual(model.barText(bat, false), "-- W")
  assert.strictEqual(model.breakdown(bat).find(r => r.label === "Total is").value, "Measured (incomplete)")
  assert.ok(model.tooltipText(bat, false).includes("Measured · Battery · incomplete"))
  assert.strictEqual(model.sampleNotes(bat), "Estimate incomplete: unknown terms counted as 0 W.")
  // An older service: incomplete, no total_lower_bound key.
  const old = parsed({ incomplete: true, total_lower_bound: undefined })
  assert.strictEqual(model.barText(old, false), "53 W")
  assert.strictEqual(model.breakdown(old).find(r => r.label === "Total is").value, "Estimated (incomplete)")
})

test("a psys total is measured platform power on AC, never battery or estimated", () => {
  const ps = parsed({ total_source: "psys", total_kind: "measured", psys_w: 64.2, total_w: 64.2 })
  assert.strictEqual(model.totalLabel(ps), "Measured (platform power)")
  assert.strictEqual(model.statusLine(ps), "Measured (platform power) · AC · Below the 150 W threshold")
  const tip = model.tooltipText(ps, false)
  assert.ok(tip.startsWith("Adapter 64 W"), tip)
  assert.ok(tip.includes("Measured (platform power) · AC"), tip)
  assert.ok(!/battery|estimated/i.test(tip.split("\n")[1]), tip)
  // One wording on every surface: status line, tooltip, "Total is", legend.
  const total = model.breakdown(ps).find(r => r.label === "Total is").value
  assert.strictEqual(total, model.PSYS_LABEL)
  assert.strictEqual(model.PSYS_LABEL, "Measured (platform power)")
  assert.ok(/platform power/.test(model.PSYS_BAND_LABEL) && /^AC, measured/.test(model.PSYS_BAND_LABEL))
  assert.strictEqual(model.breakdown(parsed({ total_source: "psys", total_kind: "measured", incomplete: true }))
    .find(r => r.label === "Total is").value, "Measured (platform power) (incomplete)")
  for (const file of ["Model.js", "BarWidget.qml"]) {
    const code = fs.readFileSync(path.join(pluginDir, file), "utf8").replace(/\/\/.*$/gm, "")
    assert.ok(!/"[^"\n]*RAPL psys[^"\n]*"/.test(code), file + " shows a second psys wording")
    assert.ok(!/"AC, measured"/.test(code), file + " has the bare legend label")
  }
  assert.strictEqual(model.sampleNotes(ps), "")
  // The model total and a battery total keep their words.
  assert.strictEqual(model.totalLabel(parsed()), "Estimated")
  assert.strictEqual(model.totalLabel(parsed({ ac_online: false, total_kind: "measured", total_source: "battery" })), "Measured")
})

test("gpu_status: suspended, none and disabled are words, not 0 W", () => {
  const row = over => model.gpuRowValue(parsed(over))
  assert.strictEqual(row({}), "15 W (P5)")
  assert.strictEqual(row({ gpu_status: "suspended", gpu_w: 0, gpu_pstate: null }), "Suspended")
  assert.strictEqual(row({ gpu_status: "none", gpu_w: 0, gpu_devices: [] }), "No discrete GPU")
  assert.strictEqual(row({ gpu_status: "disabled", gpu_w: 0, gpu_devices: [] }), "Not monitored")
  assert.strictEqual(row({ gpu_status: "unknown", gpu_w: null }), "Unknown")
  assert.strictEqual(row({ gpu_status: "unknown", gpu_w: 12 }), "12 W + unknown")
  const two = [{ pci: "0000:01:00.0", vendor: "nvidia", status: "active", power_w: 15 },
    { pci: "0000:03:00.0", vendor: "amd", status: "suspended", power_w: null }]
  assert.strictEqual(row({ gpu_devices: two }), "15 W (P5, 1 of 2 active)")
  assert.strictEqual(row({ gpu_devices: [two[0], two[0]], gpu_pstate: null }), "15 W (2 GPUs)")
  // The P-state is one GPU's: never next to the watts of several active ones.
  assert.strictEqual(row({ gpu_devices: [two[0], two[0]], gpu_pstate: "P5", gpu_w: 30 }), "30 W (2 GPUs)")
  assert.strictEqual(row({ gpu_status: "suspended", gpu_devices: [two[1], two[1]] }), "Suspended (2 GPUs)")
  // The caps section follows the same words when there is no limit to show.
  const cap = over => model.capStatus(parsed(Object.assign({ gpu_limit_w: null }, over))).gpu
  assert.strictEqual(cap({ gpu_status: "suspended" }), "Suspended")
  assert.strictEqual(cap({ gpu_status: "none" }), "No discrete GPU")
  assert.strictEqual(cap({ gpu_status: "disabled" }), "Not monitored")
  assert.strictEqual(cap({ gpu_status: "unknown" }), "Unknown")
  assert.strictEqual(cap({}), "Unknown")
})

test("unknown adapter voltage and rating: said in words, no amps, no share of rating", () => {
  const c = parsed({ adapter_v: null, adapter_current_a: null, adapter_rating_w: null })
  const get = label => model.breakdown(c).find(r => r.label === label).value
  assert.strictEqual(get("Adapter current"), "Not configured")
  assert.strictEqual(get("Of adapter rating"), "Not configured")
  // Keys absent altogether (an older service) are not configured either.
  const old = sample()
  delete old.adapter_v
  delete old.adapter_rating_w
  const o = model.breakdown(model.parseCurrent(JSON.stringify(old)).data)
  assert.strictEqual(o.find(r => r.label === "Adapter current").value, "Not configured")
  assert.strictEqual(o.find(r => r.label === "Of adapter rating").value, "Not configured")
  const tip = model.tooltipText(c, false)
  assert.strictEqual(tip.split("\n")[0], "Adapter 53 W", "no amps in the tooltip head")
  // Threshold behaviour is untouched: threshold_w is always present.
  assert.strictEqual(model.level(c.totalW, c.thresholdW), "normal")
  assert.strictEqual(model.level(parsed({ total_w: 140, threshold_w: 100, adapter_rating_w: null }).totalW, 100), "over")
  // Nonsense voltages are unknown, not a divisor.
  assert.strictEqual(parsed({ adapter_v: 0 }).adapterV, null)
  assert.strictEqual(parsed({ adapter_v: "19.5" }).adapterV, null)
  // Known voltage, amps unknown on battery: the placeholder, as before.
  const bat = parsed({ ac_online: false, adapter_current_a: null })
  assert.strictEqual(model.breakdown(bat).find(r => r.label === "Adapter current").value, "-- A")
  // No sample at all keeps the placeholders, not "not configured".
  const none = model.breakdown(null)
  assert.strictEqual(none.find(r => r.label === "Adapter current").value, "-- A")
  assert.strictEqual(none.find(r => r.label === "Of adapter rating").value, "--")
})

test("out-of-range adapter voltage or rating reads Invalid, not Not configured", () => {
  const get = (over, label) => model.breakdown(parsed(over)).find(r => r.label === label).value
  for (const v of [5000, 0, -1, "19.5", true, {}])
    assert.strictEqual(get({ adapter_v: v, adapter_current_a: 3 }, "Adapter current"), "Invalid", JSON.stringify(v))
  for (const v of [1e6, 0, -1, "330", true, {}])
    assert.strictEqual(get({ adapter_rating_w: v }, "Of adapter rating"), "Invalid", JSON.stringify(v))
  assert.strictEqual(get({ adapter_v: null }, "Adapter current"), "Not configured")
  assert.strictEqual(get({ adapter_rating_w: null }, "Of adapter rating"), "Not configured")
  assert.strictEqual(get({}, "Adapter current"), "2.7 A")
})

test("a measured total is never a lower bound, whatever the flag says", () => {
  const lb = over => model.isLowerBound(parsed(Object.assign({ total_lower_bound: true, incomplete: true }, over)))
  assert.strictEqual(lb({ total_source: "psys", total_kind: "measured" }), false)
  assert.strictEqual(lb({ total_source: "battery", total_kind: "measured", ac_online: false }), false)
  assert.strictEqual(lb({ total_source: "model", total_kind: "measured" }), false)
  assert.strictEqual(lb({ total_source: "psys", total_kind: "estimated" }), false)
  assert.strictEqual(lb({ total_source: "model", total_kind: "estimated" }), true)
  assert.strictEqual(lb({ total_source: undefined, total_kind: "estimated" }), true, "older service: no source key")
  const ps = parsed({ total_source: "psys", total_kind: "measured", total_lower_bound: true, total_w: 100 })
  assert.strictEqual(model.barText(ps, false), "100 W")
  assert.ok(!model.tooltipText(ps, false).includes(model.LOWER_BOUND_MARK))
  assert.strictEqual(model.statusLine(ps), "Measured (platform power) · AC · Below the 150 W threshold")
})

test("no shown string carries NaN, null or undefined, whatever the new keys hold", () => {
  const junk = [null, undefined, "x", NaN, {}, [], true, -1, 1e300]
  for (const v of junk) {
    const c = parsed({ total_source: v, total_lower_bound: v, psys_w: v, gpu_status: v, gpu_devices: v,
      adapter_v: v, adapter_current_a: v, adapter_rating_w: v, total_w: v, gpu_w: v })
    const texts = [model.barText(c, false), model.barLines(c, false).join(" "),
      model.tooltipText(c, false), model.statusLine(c), model.sampleNotes(c),
      model.currentStatText(model.stats([], c, false), c, false),
      model.capStatus(c).gpu, model.capStatus(c).cpu]
    for (const row of model.breakdown(c)) texts.push(row.label + ": " + row.value)
    for (const t of texts) assertClean(t, String(v))
  }
  for (const status of model.GPU_STATUSES) {
    const c = parsed({ gpu_status: status, gpu_w: null, gpu_limit_w: null, gpu_pstate: null })
    assertClean(model.gpuRowValue(c), status)
    assert.notStrictEqual(model.gpuRowValue(c), "0 W", status)
  }
})

// ---------------------------------------------------------------- history

test("parses JSONL, skipping torn, garbage, blank and non-advancing lines", () => {
  const text = [
    JSON.stringify(point(100)),
    "",
    "not json at all",
    JSON.stringify(point(102)),
    JSON.stringify(point(102, { avg: 1 })),          // t does not advance
    JSON.stringify(point(101)),                       // t goes backwards
    "[1,2,3]",
    JSON.stringify({ n: 1, avg: 3 }),                 // no t
    JSON.stringify(point(104)),
    '{"t": 106, "n": 1, "avg": 8'                     // torn tail
  ].join("\n")
  const r = model.parseHistory(text)
  assert.deepStrictEqual(r.points.map(p => p.t), [100, 102, 104])
  assert.strictEqual(r.skipped, 6)
  assert.strictEqual(r.truncated, false)
})

test("points stamped after now are ignored and do not hold back later ones", () => {
  const text = jsonl([point(100), point(1e10), point(102), point(5000), point(104)])
  const r = model.parseHistory(text, 1000)
  assert.deepStrictEqual(r.points.map(p => p.t), [100, 102, 104])
  assert.strictEqual(r.skipped, 0)
  assert.deepStrictEqual(model.parseHistory(text).points.map(p => p.t), [100, 1e10], "without now: the 1e10 line blocks the rest")
})

test("history points are normalized and bound", () => {
  const r = model.parseHistory(JSON.stringify({ t: 5, n: "3", avg: "1", max: 1e12, ac: 2, k: "z", extra: 1 }) + "\n")
  assert.deepStrictEqual(r.points[0], { t: 5, n: 1, avg: null, max: null, cpu: null, gpu: null,
    rest: null, chg: null, ac: null, k: "" })
})

test("history caps keep the NEWEST: character budget, overlong lines, line count", () => {
  const long = JSON.stringify(point(1, { pad: "y".repeat(model.HISTORY_MAX_LINE_CHARS) }))
  assert.strictEqual(model.parseHistory(long + "\n" + JSON.stringify(point(2))).skipped, 1)
  const total = model.HISTORY_MAX_LINES + 10
  const many = Array.from({ length: total }, (_, i) => JSON.stringify({ t: i + 1 })).join("\n") + "\n"
  const r = model.parseHistory(many)
  assert.strictEqual(r.points.length, model.HISTORY_MAX_LINES)
  assert.strictEqual(r.points[r.points.length - 1].t, total, "the newest line is kept")
  assert.strictEqual(r.points[0].t, 11, "the oldest ten are dropped")
  assert.strictEqual(r.truncated, true)
  // A budget cut lands on a line start: no torn first line is counted.
  const big = "x".repeat(model.HISTORY_PARSE_CHARS) + "\n" + JSON.stringify(point(7)) + "\n"
  const b = model.parseHistory(big)
  assert.deepStrictEqual(b.points.map(p => p.t), [7])
  assert.strictEqual(b.skipped, 0)
  assert.strictEqual(b.truncated, true)
})

test("a sub-0.12 s interval at the compaction ceiling still charts its newest part", () => {
  // Raw tier at 0.1 s: 1.25 x 3600 / 0.1 = 45 000 lines of ~109 B, past the
  // parse budget. Before the fix this blanked the 1h chart ("too large").
  const now = 1761300000
  const lines = []
  for (let i = 0; i < 45000; i++) lines.push(JSON.stringify({ t: Math.round((now - 4500 + i * 0.1) * 100) / 100,
    n: 1, avg: 80.12, max: 90.45, cpu: 8.21, gpu: 38.05, rest: 25.01, chg: 0.0, ac: 1.0, k: "e" }))
  const text = lines.join("\n") + "\n"
  assert.ok(text.length > model.HISTORY_PARSE_CHARS && text.length < model.HISTORY_MAX_BYTES, String(text.length))
  const t0 = Date.now()
  const r = model.parseHistory(text, now)
  const m = model.chartModel(r.points, "1h", now, 0.1, 150)
  const ms = Date.now() - t0
  assert.strictEqual(r.truncated, true)
  assert.ok(r.points.length > 30000, String(r.points.length))
  assert.strictEqual(r.points[r.points.length - 1].t, JSON.parse(lines[lines.length - 1]).t)
  assert.ok(m.count > 0 && m.runs.length >= 1)
  assert.ok(ms < 1000, "took " + ms + " ms")
})

test("parsing a tier at its compaction ceiling is fast", () => {
  const lines = []
  for (let i = 0; i < 3600; i++) lines.push(JSON.stringify(point(1761300000 + i * 60)))
  const t0 = Date.now()
  const r = model.parseHistory(lines.join("\n"))
  const ms = Date.now() - t0
  assert.strictEqual(r.points.length, 3600)
  assert.ok(ms < 200, "took " + ms + " ms")
})

test("range filter keeps [from, to]", () => {
  const pts = [point(10), point(20), point(30), point(40)]
  assert.deepStrictEqual(model.filterRange(pts, 20, 30).map(p => p.t), [20, 30])
  assert.deepStrictEqual(model.filterRange(null, 0, 1), [])
})

test("gaps: more than 2.5 steps apart breaks a run", () => {
  const pts = [point(0), point(2), point(4), point(9), point(11), point(16.1)]
  assert.deepStrictEqual(model.splitGaps(pts, 2).map(r => r.map(p => p.t)), [[0, 2, 4, 9, 11], [16.1]])
  assert.deepStrictEqual(model.splitGaps([point(0), point(5.1)], 2).length, 2)
  assert.deepStrictEqual(model.splitGaps([point(0), point(5)], 2).length, 1)
  assert.deepStrictEqual(model.splitGaps([], 2), [])
})

test("power mode: ac=0 is battery, k=e is AC, k=x is mixed", () => {
  assert.strictEqual(model.pointMode(point(0, { k: "m", ac: 0 })), "battery")
  assert.strictEqual(model.pointMode(point(0, { k: "", ac: 0 })), "battery")
  assert.strictEqual(model.pointMode(point(0, { k: "e" })), "ac")
  assert.strictEqual(model.pointMode(point(0, { k: "x", ac: 0.5 })), "mixed")
  assert.strictEqual(model.pointMode(point(0, { k: "", ac: 1 })), "ac")
  assert.strictEqual(model.pointMode(point(0, { k: "", ac: null })), "unknown")
})

test("power mode: k=m on AC is measured AC (psys), never battery", () => {
  assert.strictEqual(model.pointMode(point(0, { k: "m", ac: 1 })), "acMeasured")
  // The battery is measured only when AC is known to be off: no AC state is psys.
  assert.strictEqual(model.pointMode(point(0, { k: "m", ac: null })), "acMeasured")
  assert.strictEqual(model.pointMode(point(0, { k: "m", ac: 0.5 })), "mixed")
  assert.strictEqual(model.pointMode(point(0, { k: "e", ac: 1 })), "ac")
  const pts = [point(0, { k: "m", ac: 1 }), point(2, { k: "m", ac: 1 }), point(4, { k: "m", ac: 0 })]
  assert.deepStrictEqual(model.modeSpans(pts, 2).map(s => s.mode), ["acMeasured", "battery"])
  // The legend shows the measured-AC band only when there is one to explain.
  const now = 1761300000
  const psysChart = model.chartModel([point(now - 10, { k: "m", ac: 1 })], "1h", now, 2, 150)
  const modelChart = model.chartModel([point(now - 10)], "1h", now, 2, 150)
  assert.strictEqual(model.showsMeasuredAc(psysChart, parsed()), true)
  assert.strictEqual(model.showsMeasuredAc(modelChart, parsed()), false)
  assert.strictEqual(model.showsMeasuredAc(null, parsed({ total_source: "psys" })), true)
  assert.strictEqual(model.showsMeasuredAc(null, null), false)
  assert.ok(model.CHART_STYLE.acMeasuredAlpha > model.CHART_STYLE.mixedAlpha
    && model.CHART_STYLE.acMeasuredAlpha < model.CHART_STYLE.batteryAlpha)
})

test("merged slots keep psys with unknown AC apart from battery", () => {
  // The mean of ac skips nulls: k=m ac=null (psys, AC state unknown) with
  // k=m ac=0 (battery) averaged to ac 0 and drew as battery.
  const mixed = model.consolidate([point(0, { k: "m", ac: null }), point(2, { k: "m", ac: null }),
    point(3, { k: "m", ac: 0 })], 0, 10)
  assert.strictEqual(mixed.length, 1)
  assert.strictEqual(model.pointMode(mixed[0]), "mixed")
  const psys = model.consolidate([point(0, { k: "m", ac: null }), point(2, { k: "m", ac: 1 })], 0, 10)
  assert.strictEqual(model.pointMode(psys[0]), "acMeasured")
  const bat = model.consolidate([point(0, { k: "m", ac: 0 }), point(2, { k: "", ac: 0 })], 0, 10)
  assert.strictEqual(model.pointMode(bat[0]), "battery")
  // A history line cannot set the mode itself.
  assert.strictEqual(model.parseHistory(JSON.stringify(point(5, { mode: "battery", ac: 1 })) + "\n").points[0].mode, undefined)
  // Through the whole chart: past the render cap, a psys hour with one
  // battery minute never turns into a battery band where it is psys.
  const now = 1761300000
  const pts = []
  for (let i = 0; i < 3000; i++) pts.push(point(now - 3600 + i * 1.2, { k: "m", ac: i >= 1500 && i < 1550 ? 0 : null }))
  const spans = model.chartModel(pts, "1h", now, 1.2, 150).spans
  const battery = spans.filter(sp => sp.mode === "battery").reduce((a, sp) => a + sp.t1 - sp.t0, 0)
  assert.ok(battery <= 50 * 1.2 + 5, "battery band " + battery + " s for 60 s of battery")
})

test("mode spans follow runs and break at gaps", () => {
  const pts = [
    point(0, { k: "e" }), point(2, { k: "e" }),
    point(4, { k: "m", ac: 0 }), point(6, { k: "m", ac: 0 }),
    point(20, { k: "m", ac: 0 })
  ]
  assert.deepStrictEqual(model.modeSpans(pts, 2), [
    { mode: "ac", t0: 0, t1: 4 },
    { mode: "battery", t0: 4, t1: 8 },
    { mode: "battery", t0: 20, t1: 22 }
  ])
})

test("stats: max of max, n-weighted mean of avg, current only while fresh", () => {
  const pts = [
    point(0, { n: 30, avg: 100, max: 120 }),
    point(60, { n: 10, avg: 60, max: 200 }),
    point(120, { n: 5, avg: null, max: null })
  ]
  const cur = { totalW: 77 }
  assert.deepStrictEqual(model.stats(pts, cur, false), { max: 200, avg: 90, current: 77 })
  assert.deepStrictEqual(model.stats(pts, cur, true), { max: 200, avg: 90, current: null })
  assert.deepStrictEqual(model.stats([], null, false), { max: null, avg: null, current: null })
  // a point with avg but no max still counts toward Max
  assert.strictEqual(model.stats([point(0, { avg: 50, max: null })], null, true).max, 50)
})

test("consolidation merges time slots: mean of avg (n-weighted), max of max, mixed k", () => {
  const run = [point(0, { n: 1, avg: 10, max: 10 }), point(2, { n: 3, avg: 30, max: 90, k: "m", ac: 0 }), point(4, { avg: 5, max: 5 })]
  const out = model.consolidate(run, 0, 4)
  assert.strictEqual(out.length, 2)
  assert.strictEqual(out[0].t, 0)
  assert.strictEqual(out[0].tEnd, 2)
  assert.strictEqual(out[0].n, 4)
  assert.strictEqual(out[0].avg, 25)
  assert.strictEqual(out[0].max, 90)
  assert.strictEqual(out[0].k, "x")
  assert.strictEqual(out[0].ac, 0.25)
  assert.strictEqual(out[1], run[2], "a slot of one keeps the point as is")
  assert.strictEqual(model.consolidate(run, 0, 0), run)
})

test("a gap wider than a slot survives consolidation; one inside a slot merges away", () => {
  const pts = [point(0), point(1), point(2), point(3), point(40), point(41)]
  const merged = model.consolidate(pts, 0, 10)
  assert.strictEqual(merged.length, 2)
  assert.strictEqual(model.splitGaps(merged, 2).length, 2, "the 37 s hole is still a gap")
  // 3 -> 8 is a gap at step 1, but within one 10 s slot it is below the chart's resolution
  const inSlot = model.consolidate([point(0), point(3), point(8), point(9)], 0, 10)
  assert.strictEqual(model.splitGaps(inSlot, 1).length, 1)
})

test("nice axis always clears the top value", () => {
  assert.deepStrictEqual(model.niceAxis(157.5), { max: 200, step: 50, ticks: [0, 50, 100, 150, 200] })
  assert.strictEqual(model.niceAxis(100).max > 100, true)
  assert.strictEqual(model.niceAxis(0).max > 0, true)
  assert.strictEqual(model.niceAxis(NaN).max > 0, true)
})

test("time ticks: 10 min for 1h, 3 h for 24h, midnights for 7d", () => {
  const now = Date.UTC(2026, 9, 24, 13, 47, 0) / 1000
  const h = model.timeTicks(now - 3600, now, "1h")
  assert.deepStrictEqual(h.map(t => t.label), ["12:50", "13:00", "13:10", "13:20", "13:30", "13:40"])
  const d = model.timeTicks(now - 86400, now, "24h")
  assert.strictEqual(d[0].label, "15:00")
  assert.strictEqual(d.length, 8)
  const w = model.timeTicks(now - 604800, now, "7d")
  assert.deepStrictEqual(w.map(t => t.label), ["Sun 18", "Mon 19", "Tue 20", "Wed 21", "Thu 22", "Fri 23", "Sat 24"])
  assert.deepStrictEqual(model.timeTicks(5, 5, "1h"), [])
})

test("chart model: filters to the range, keeps gaps, scales above threshold", () => {
  const now = 1761300000
  const pts = []
  for (let i = 0; i < 100; i++) pts.push(point(now - 4000 + i * 2))     // older than the 1h range
  for (let i = 0; i < 50; i++) pts.push(point(now - 600 + i * 2, { avg: 170, max: 180 }))
  for (let i = 0; i < 50; i++) pts.push(point(now - 200 + i * 2, { k: "m", ac: 0 }))
  const m = model.chartModel(pts, "1h", now, 2, 150)
  assert.strictEqual(m.count, 100)
  assert.strictEqual(m.runs.length, 2, "the 400 s hole is a gap")
  assert.strictEqual(m.step, 2)
  assert.strictEqual(m.offset, 0)
  assert.ok(m.axis.max >= 180)
  assert.deepStrictEqual(m.spans.map(s => s.mode), ["ac", "battery"])
  const day = model.chartModel([point(now - 120), point(now - 60)], "24h", now, 2, 150)
  assert.strictEqual(day.step, 60)
  assert.strictEqual(day.offset, 30)
  assert.ok(day.axis.max > 150, "threshold always visible")
})

test("render cap is global: gap-heavy history draws at most the cap, in linear time", () => {
  // Every point isolated by a gap (the audit's quadratic case: 27 685 runs took
  // 86 s per paint in V4). Points 0.13 s apart at interval 0.05 s.
  const now = 1761300000
  const pts = []
  for (let t = now - 3599; t < now && pts.length < 30000; t += 0.13) pts.push(point(Math.round(t * 100) / 100))
  assert.ok(pts.length >= 27000, String(pts.length))
  const t0 = Date.now()
  const m = model.chartModel(pts, "1h", now, 0.05, 150)
  const lay = model.chartLayout(m, 540, 200, {})
  const ms = Date.now() - t0
  const drawn = m.runs.reduce((a, r) => a + r.length, 0)
  assert.ok(drawn <= model.MAX_RENDER_POINTS, "drew " + drawn)
  assert.ok(m.runs.length <= model.MAX_RENDER_POINTS, "runs " + m.runs.length)
  assert.ok(m.spans.length <= model.MAX_RENDER_POINTS, "spans " + m.spans.length)
  assert.ok(lay.area.length <= model.MAX_RENDER_POINTS, "polys " + lay.area.length)
  assert.strictEqual(m.count, pts.length)
  assert.ok(ms < 100, "chartModel + chartLayout took " + ms + " ms")
  // Alternating battery/AC every point: spans are capped too.
  const alt = pts.map((p, i) => Object.assign({}, p, i % 2 ? { k: "m", ac: 0 } : { k: "e", ac: 1 }))
  assert.ok(model.chartModel(alt, "1h", now, 0.13, 150).spans.length <= model.MAX_RENDER_POINTS)
})

test("chartLayout appends, never concats (linear in the number of runs)", () => {
  const src = model.chartLayout.toString()
  assert.ok(!/\.concat\(/.test(src), "chartLayout must not concat inside its run loop")
})

test("chart model consolidates dense data down to the render cap", () => {
  const now = 1761300000
  const pts = []
  for (let i = 0; i < 7200; i++) pts.push(point(now - 3600 + i * 0.5))
  const m = model.chartModel(pts, "1h", now, 0.5, 150)
  const drawn = m.runs.reduce((a, r) => a + r.length, 0)
  assert.ok(drawn <= model.MAX_RENDER_POINTS, "drew " + drawn)
  assert.strictEqual(m.runs.length, 1)
})

test("layout: gaps and null values break lines, lone points get a width", () => {
  const now = 1000
  const pts = [point(100), point(102), point(104, { cpu: null }), point(106), point(500)]
  const m = model.chartModel(pts, "1h", now + 2600, 2, 150)
  // re-anchor so the range covers the points
  const lay = model.chartLayout(m, 600, 200, { left: 40, right: 8, top: 8, band: 6, bottom: 20 })
  assert.strictEqual(lay.area.length, 2)
  assert.strictEqual(lay.cpu.length, 3, "null cpu splits the first run")
  assert.strictEqual(lay.area[1].length, 2, "lone point drawn one step wide")
  assert.ok(lay.area[1][1][0] > lay.area[1][0][0])
  assert.ok(lay.thresholdY > lay.plot.y && lay.thresholdY < lay.baseY)
  assert.strictEqual(lay.empty, false)
  for (const poly of lay.max) for (const [x, y] of poly) {
    assert.ok(x >= lay.plot.x - 0.01 && x <= lay.plot.x + lay.plot.w + 2, "x in plot: " + x)
    assert.ok(y >= lay.plot.y - 0.01 && y <= lay.baseY + 0.01, "y in plot: " + y)
  }
  assert.ok(lay.yTicks.length >= 3 && lay.xTicks.length >= 1)
  assert.ok(/ W$/.test(lay.yTicks[lay.yTicks.length - 1].label), "the top watt tick carries the unit")
  assert.ok(!/W/.test(lay.yTicks[0].label))
})

test("axis labels are clamped by their own width, so 'Sat 24' fits at the edge", () => {
  // a 7d label ~40 px wide whose tick sits 5 px inside the plot's right edge
  assert.strictEqual(model.clampLabelX(495, 40, 40, 460), 480)
  assert.strictEqual(model.clampLabelX(42, 40, 40, 460), 60)
  assert.strictEqual(model.clampLabelX(200, 40, 40, 460), 200)
  assert.strictEqual(model.clampLabelX(200, 1000, 40, 460), 270, "a label wider than the plot is centred")
})

test("Max, CPU and GPU differ by pattern or weight, not only colour", () => {
  const st = model.CHART_STYLE
  const sig = (dash, width) => JSON.stringify([dash || null, width])
  const looks = [sig(st.maxDash, st.maxWidth), sig(null, st.cpuWidth), sig(st.gpuDash, st.gpuWidth)]
  assert.strictEqual(new Set(looks).size, 3, looks.join(" "))
  assert.notDeepStrictEqual(st.maxDash, st.gpuDash)
  assert.ok(st.gpuDash[0] >= 8, "GPU dashes are long")
  assert.ok(st.cpuWidth > st.maxWidth && st.gpuWidth > st.maxWidth)
})

test("layout of an empty range says so", () => {
  const m = model.chartModel([], "7d", 1761300000, 2, 150)
  const lay = model.chartLayout(m, 600, 200)
  assert.strictEqual(lay.empty, true)
  assert.deepStrictEqual(lay.area, [])
  assert.strictEqual(model.chartLayout(null, 600, 200).empty, true)
})

test("dashes alternate along a polyline and carry across vertices", () => {
  const segs = model.dashSegments([[0, 0], [10, 0], [10, 10]], 4, 3)
  // 0-4 on, 4-7 off, 7-10 + 0-1 down on (carried), 1-4 off, 4-8 on, 8-10 off
  assert.deepStrictEqual(segs.map(s => s.map(v => Math.round(v * 100) / 100)), [
    [0, 0, 4, 0], [7, 0, 10, 0], [10, 0, 10, 1], [10, 4, 10, 8]])
  assert.deepStrictEqual(model.dashSegments([[0, 0]], 4, 3), [])
  assert.deepStrictEqual(model.dashSegments(null, 4, 3), [])
  assert.strictEqual(model.dashSegments([[0, 0], [10000, 0]], 2, 2, 7).length, 7, "budget honoured")
  assert.strictEqual(model.dashSegments([[0, 0], [1e7, 0]], 1, 1).length, model.MAX_DASH_SEGMENTS)
})

test("ranges map to tiers and files; state dir honours the env override", () => {
  assert.deepStrictEqual(model.RANGES.map(r => [r.key, r.tier, r.file]), [
    ["1h", "raw", "history-raw.jsonl"], ["24h", "1m", "history-1m.jsonl"], ["7d", "15m", "history-15m.jsonl"]])
  assert.strictEqual(model.rangeFor("bogus").key, "1h")
  assert.strictEqual(model.stepFor("1h", 2.5), 2.5)
  assert.strictEqual(model.stepFor("1h", null), 2)
  assert.strictEqual(model.stepFor("7d", 2), 900)
  assert.strictEqual(model.historyPath("/s", "24h"), "/s/history-1m.jsonl")
})

test("state dir: same precedence as bin/power-watch state_dir()", () => {
  // POWER_WATCH_STATE_DIR > XDG_STATE_HOME/power-watch > ~/.local/state/power-watch
  assert.strictEqual(model.stateDir("", "", "/home/u"), "/home/u/.local/state/power-watch")
  assert.strictEqual(model.stateDir(undefined, undefined, "/home/u"), "/home/u/.local/state/power-watch")
  assert.strictEqual(model.stateDir(null, "/x", "/home/u"), "/x/power-watch")
  assert.strictEqual(model.stateDir("", "/x/", "/home/u"), "/x/power-watch")
  assert.strictEqual(model.stateDir("", "/", "/home/u"), "/power-watch")
  assert.strictEqual(model.stateDir("/tmp/pw/", "/x", "/home/u"), "/tmp/pw")
  assert.strictEqual(model.stateDir("/", "/x", "/home/u"), "/")
  // no trimming, as in Python: a space is part of the path
  assert.strictEqual(model.stateDir(" /tmp/pw", "/x", "/home/u"), " /tmp/pw")
})

test("state dir matches bin/power-watch for the same environment", () => {
  const { execFileSync } = require("child_process")
  const bin = path.join(__dirname, "..", "..", "bin", "power-watch")
  let python = null
  for (const name of ["python3", "python"]) {
    try { execFileSync(name, ["-c", "pass"]); python = name; break } catch (e) { /* next */ }
  }
  if (!python || !fs.existsSync(bin)) return
  // Load the script as a module (it has no .py suffix) and print state_dir().
  const probe = [
    "import importlib.machinery, importlib.util, sys",
    "loader = importlib.machinery.SourceFileLoader('pw', sys.argv[1])",
    "spec = importlib.util.spec_from_loader('pw', loader)",
    "mod = importlib.util.module_from_spec(spec)",
    "loader.exec_module(mod)",
    "print(mod.state_dir())"
  ].join("\n")
  const cases = [
    { POWER_WATCH_STATE_DIR: "/tmp/pw/", XDG_STATE_HOME: "/x" },
    { POWER_WATCH_STATE_DIR: "", XDG_STATE_HOME: "/x/y/" },
    { POWER_WATCH_STATE_DIR: "", XDG_STATE_HOME: "" }
  ]
  for (const env of cases) {
    const full = Object.assign({}, process.env, env, { HOME: "/home/u" })
    const py = execFileSync(python, ["-c", probe, bin], { env: full, encoding: "utf8" }).trim()
    assert.strictEqual(model.stateDir(env.POWER_WATCH_STATE_DIR, env.XDG_STATE_HOME, "/home/u"), py, JSON.stringify(env))
  }
})

test("file URLs percent-encode each path segment", () => {
  assert.strictEqual(model.fileUrl("/home/u/.local/state/power-watch"), "file:///home/u/.local/state/power-watch")
  assert.strictEqual(model.fileUrl("/tmp/a b#c%d?e"), "file:///tmp/a%20b%23c%25d%3Fe")
})

// ---------------------------------------------------------------- QML source

// qmllint cannot see these; a renamed helper only fails on the click that calls it.
const qmlFiles = fs.readdirSync(pluginDir).filter(f => f.endsWith(".qml"))
// Source with // comments removed: the header comments name things on purpose.
const qmlCode = file => fs.readFileSync(path.join(pluginDir, file), "utf8").replace(/\/\/.*$/gm, "")

test("every Model.<name> the QML calls exists in Model.js", () => {
  for (const file of qmlFiles) {
    const qml = qmlCode(file).replace(/^import\s+.*$/gm, "")
    for (const m of qml.matchAll(/\bModel\.([A-Za-z_]\w*)/g)) {
      assert.ok(NAMES.includes(m[1]), file + " uses Model." + m[1] + ", not in Model.js (or not lifted by this test)")
    }
  }
})

test("every Text declares textFormat: Text.PlainText", () => {
  for (const file of qmlFiles) {
    const qml = qmlCode(file)
    const texts = (qml.match(/(^|[\s:])Text\s*\{/gm) || []).length
    const plain = (qml.match(/textFormat:\s*Text\.PlainText/g) || []).length
    assert.strictEqual(plain, texts, file + ": " + texts + " Text items, " + plain + " PlainText declarations")
  }
})

test("theme colours are read as Commons.Color, never a bare Color", () => {
  // Under the shell's engine a bare `Color` resolves to another object whose
  // .foreground is undefined (measured in a scratch qs); qs.Commons' singleton
  // has to be reached through the qualified import.
  for (const file of qmlFiles) {
    const qml = qmlCode(file)
    assert.ok(!/(^|[^.\w])Color\.\w/m.test(qml), file + " reads a bare Color.<role>")
    if (/Commons\.Color/.test(qml)) assert.ok(/^import qs\.Commons as Commons$/m.test(qml), file + " lacks the Commons import")
  }
})

test("no Process anywhere, and the only repeating timer is the staleness check", () => {
  for (const file of qmlFiles) {
    const qml = qmlCode(file)
    assert.ok(!/\bProcess\s*\{/.test(qml), file + " declares a Process")
    assert.ok(!/execDetached|bar\.run\(/.test(qml), file + " spawns a command")
    const repeats = (qml.match(/repeat:\s*true/g) || []).length
    if (file === "BarWidget.qml") {
      assert.strictEqual(repeats, 1, "BarWidget.qml should have exactly one repeating Timer")
      assert.ok(/interval:\s*Model\.STALE_CHECK_MS/.test(qml), "the repeating Timer runs at STALE_CHECK_MS")
    } else {
      assert.strictEqual(repeats, 0, file + " has a repeating Timer")
    }
  }
})

test("history is only watched and read while the panel is open", () => {
  const qml = qmlCode("BarWidget.qml")
  assert.ok(/id:\s*historyLoader[\s\S]{0,200}active:\s*root\.opened/.test(qml),
    "the history reader lives in a Loader gated on root.opened")
  assert.ok(/minIntervalMs:\s*Model\.HISTORY_THROTTLE_MS/.test(qml), "history reads are throttled")
  const loader = qml.slice(qml.indexOf("id: historyLoader"))
  assert.ok(/GatedFile\s*\{[\s\S]*?maxBytes:\s*Model\.HISTORY_MAX_BYTES/.test(loader), "history goes through the read gate")
})

test("every file read goes through the size gate: FileView only in GatedFile.qml", () => {
  for (const file of qmlFiles) {
    const qml = qmlCode(file)
    const views = (qml.match(/\bFileView\s*\{/g) || []).length
    assert.strictEqual(views, file === "GatedFile.qml" ? 1 : 0, file + " declares " + views + " FileView(s)")
  }
  const gate = qmlCode("GatedFile.qml")
  // The FileView starts with no path; the only non-empty path assignment is
  // the one settle() makes after gateDecision said "read".
  assert.ok(/FileView\s*\{\s*id:\s*file\s*path:\s*""/.test(gate), "the FileView starts with an empty path")
  const sets = gate.match(/file\.path\s*=\s*[^=\n]+/g) || []
  assert.deepStrictEqual(sets.map(x => x.trim()).filter(x => !/=\s*""$/.test(x)), ["file.path = gate.filePath"])
  const settle = gate.slice(gate.indexOf("function settle()"), gate.indexOf("onDirChanged"))
  assert.ok(/Model\.gateDecision\([\s\S]*decision\.action === "read"[\s\S]*file\.path = gate\.filePath/.test(settle),
    "the path is set only after the gate said read")
  assert.ok(!/file\.reload\(\)[\s\S]*Model\.gateDecision/.test(settle), "no reload before the decision")
  // text() only once the gated read has completed
  assert.strictEqual((gate.match(/\.text\(\)/g) || []).length, 1)
  assert.ok(/onLoaded:\s*\{[^}]*file\.text\(\)/.test(gate))
  assert.ok(/import Qt\.labs\.folderlistmodel/.test(gate))
  // a read inside the throttle window waits (and re-lists at its end)
  assert.ok(/decision\.action === "read"\) \{\s*if \(throttle\.running\) \{\s*gate\.waiting = true\s*return/.test(settle),
    "a read due inside the window is deferred before anything is read")
  // the staleness tick re-lists, through shouldRelist, and reads nothing itself
  const bar = qmlCode("BarWidget.qml")
  const tick = bar.slice(bar.indexOf("interval: Model.STALE_CHECK_MS"), bar.indexOf("id: historyLoader"))
  assert.ok(/Model\.shouldRelist\(currentGate\.outcome\)\)\s*currentGate\.relist\(\)/.test(tick))
  assert.ok(!/reload\(|text\(\)/.test(tick))
  assert.ok(/GatedFile\s*\{\s*id:\s*currentGate[\s\S]*?maxBytes:\s*Model\.CURRENT_MAX_BYTES/.test(bar))
})

test("chart and legend share one style, and the chart's sizes are scaled", () => {
  const chart = qmlCode("Chart.qml")
  assert.ok(/Model\.CHART_STYLE/.test(chart) && /Model\.CHART_STYLE/.test(qmlCode("BarWidget.qml")))
  assert.ok(/ctx\.font\s*=\s*Model\.canvasFont\(/.test(chart), "the Canvas font string is built by canvasFont")
  assert.ok(/Model\.clampLabelX\(/.test(chart), "time labels are clamped by their measured width")
  // no bare numeric dash patterns or label clamps left in the paint code
  assert.ok(!/strokeDashed\([^)]*,\s*\d+\s*,\s*\d+\s*\)/.test(chart), "dash lengths come from CHART_STYLE")
  assert.ok(!/plot\.x \+ 14/.test(chart))
})

test("stale data is dimmed with opacity and the panel scrolls", () => {
  const bar = qmlCode("BarWidget.qml")
  assert.ok(!/Qt\.darker/.test(bar), "dimming is opacity, which recedes on light themes too")
  assert.ok(/text:\s*Model\.sampleTitle\(root\.current, root\.stale\)/.test(bar))
  assert.ok(/id:\s*breakdownGrid[\s\S]{0,300}opacity:\s*root\.stale \? 0\.45 : 1/.test(bar))
  assert.ok(/Flickable\s*\{\s*id:\s*panelFlick/.test(bar), "the panel content is in a Flickable")
  assert.ok(/elide:\s*Text\.ElideRight/.test(bar) && !/ElideLeft/.test(bar))
  assert.ok(/flow:\s*Grid\.TopToBottom/.test(bar))
  assert.ok(/label:\s*"Mixed"/.test(bar), "the legend has a Mixed band entry")
})

test("every band mode the model produces has a colour in the chart", () => {
  const chart = qmlCode("Chart.qml")
  for (const mode of ["battery", "mixed", "acMeasured", "ac"])
    assert.ok(new RegExp('mode === "' + mode + '"').test(chart), "Chart.qml spanColor lacks " + mode)
  const bar = qmlCode("BarWidget.qml")
  assert.ok(/visible:\s*Model\.showsMeasuredAc\(root\.chart, root\.current\);\s*label:\s*Model\.PSYS_BAND_LABEL/.test(bar))
  assert.ok(/id:\s*widest[\s\S]{0,200}text:\s*Model\.BAR_WIDEST_TEXT\s*$/m.test(bar),
    "the bar always reserves room for the lower-bound mark")
  assert.ok(/fixedHeight:\s*root\.vertical \? Model\.BAR_VERTICAL_SLOTS \* Style\.bar\.iconSlot : -1/.test(bar),
    "the vertical bar is always BAR_VERTICAL_SLOTS tall")
  assert.ok(/topPadding:\s*Math\.max\(0, Model\.BAR_VERTICAL_SLOTS - root\.barLines\.length\)/.test(bar),
    "without the mark its slot stays empty above the number")
  assert.ok(/label:\s*"CURRENT";\s*value:\s*Model\.currentStatText\(/.test(bar))
})

test("no machine-specific wording or figures in the plugin", () => {
  for (const file of fs.readdirSync(pluginDir).filter(f => /\.(qml|js|json)$/.test(f))) {
    const text = fs.readFileSync(path.join(pluginDir, file), "utf8")
    assert.ok(!/\b(190|330|19\.5)\b|GTX|1080|Raul's/.test(text.replace(/"author":[^\n]*/, "")), file)
  }
  const manifest = JSON.parse(fs.readFileSync(path.join(pluginDir, "manifest.json"), "utf8"))
  assert.strictEqual(manifest.version, "1.1.0")
  assert.ok(/Estimated or measured/.test(manifest.description), manifest.description)
})

let failed = 0
for (const [name, fn] of tests) {
  try {
    fn()
    console.log("ok   " + name)
  } catch (e) {
    failed++
    console.log("FAIL " + name + "\n     " + String(e && e.message || e).split("\n").join("\n     "))
  }
}
console.log((tests.length - failed) + "/" + tests.length + " passed")
process.exit(failed ? 1 : 0)
