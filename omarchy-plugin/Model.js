// Pure model for the Power Watch bar widget: every parse, filter and number
// the QML shows comes out of a function here, so tests/plugin/model-test.js can
// cover it under node without a running shell.
//
// Written as a QML JS module (bare declarations, no exports). The node test
// evaluates the source and lifts the names out, as omavnc's model-test does.
//
// The files read here are written by the power-watch service; the shared
// contract (current.json + three JSONL tiers) is what this parses. Nothing
// from those files reaches the UI without passing a bound check below:
// numbers through finiteIn, strings through safeLabel.

// ---------------------------------------------------------------- constants

// The bar turns "approaching" at this fraction of threshold_w, and "over" at
// the threshold itself.
var APPROACH_FRACTION = 0.85

// current.json is stale (service not running) once its epoch is older than
// this many sampling intervals.
var STALE_INTERVALS = 3

// Fallbacks when current.json is missing a field or the field is nonsense.
var DEFAULT_INTERVAL_S = 2
var DEFAULT_THRESHOLD_W = 150

// The service's enums (contract §3). Anything else reads as "" (not reported).
var TOTAL_SOURCES = ["battery", "psys", "model"]
var GPU_STATUSES = ["active", "suspended", "none", "disabled", "unknown"]
var GPU_DEVICE_STATUSES = ["active", "suspended", "unknown"]
var GPU_VENDORS = ["nvidia", "amd"]

// At most this many gpu_devices entries are kept; the rest are counted, and
// the GPU row says the list was cut ("16 of 20 GPUs listed").
var MAX_GPU_DEVICES = 16

// parseGpuDevices looks at no more than this many gpu_devices entries, kept
// or not, whatever bounded the text it came from: past it the count is a floor
// ("16 of 256+ GPUs listed").
var MAX_GPU_DEVICE_SCAN = 256

// Prefixed to a total (and the figures derived from it) that is a lower
// bound: unknown terms were counted as 0 W, so the real load is at least this.
var LOWER_BOUND_MARK = "\u2265"

// What the horizontal bar reserves room for: the mark and three digits,
// always, so the bar keeps its width when the mark comes and goes.
var BAR_WIDEST_TEXT = LOWER_BOUND_MARK + "000 W"

// The vertical bar's height in icon slots, always: the mark's line, the
// number and the unit (barLines). Without the mark its slot stays empty above
// the number, so neither the bar nor the digits move when it toggles.
var BAR_VERTICAL_SLOTS = 3

// How a total measured on AC (RAPL psys) is named on every
// surface: status line, tooltip, the "Total is" row and the band legend.
var PSYS_LABEL = "Measured (platform power)"
var PSYS_BAND_LABEL = "AC, measured (platform power)"

// How often the staleness check runs, in ms. It is the only repeating timer in
// the plugin and spawns nothing: it compares two numbers, and only while
// current.json is absent does it re-list the state directory (a readdir on
// Qt's worker thread, no process). See shouldRelist.
var STALE_CHECK_MS = 10000

// Minimum gap between two current.json reads per widget (one per bar, so per
// monitor). The file is replaced once per sample and a version already read is
// never read again, so at intervals of 1 s and up this costs nothing; below
// it, the bar shows one sample a second instead of every one. The staleness
// window: the bar lags the file by at most this, and isStale allows for it.
var CURRENT_READ_MIN_MS = 1000

// Minimum gap between two history reloads while the panel is open. The raw
// tier is appended every ~2 s; the chart follows it at most this often.
var HISTORY_THROTTLE_MS = 5000

// A run of consecutive points further apart than this many steps is a gap:
// drawn as a break, never interpolated (contract "Readers").
var GAP_STEPS = 2.5

// Read caps, in bytes, applied BEFORE a file is read: GatedFile.qml lists the
// state directory and hands a file to its FileView only when the listing says
// it is no larger than this (gateDecision). A FileView has no byte limit of
// its own and materialises the whole file as one string.
// current.json is well under 2 KiB. A raw history tier at its compaction
// ceiling is ~0.5 MiB at a 2 s interval and ~10 MiB at the 0.05 s floor.
var CURRENT_MAX_BYTES = 16384
var HISTORY_MAX_BYTES = 16777216

// Parse caps on text that did pass the read gate. current.json is not parsed
// past CURRENT_MAX_CHARS (a second line of defence; UTF-8 bytes >= chars, so
// the gate already implies it). A history tier is parsed from its END: the
// newest HISTORY_PARSE_CHARS and at most HISTORY_MAX_LINES lines, so a fast
// interval shows the latest part of the range ("older points not shown")
// instead of nothing.
var CURRENT_MAX_CHARS = 16384
var HISTORY_PARSE_CHARS = 4194304
var HISTORY_MAX_LINES = 40000
var HISTORY_MAX_LINE_CHARS = 1024

// The chart never draws more than this many points per series across the
// whole range, whatever the gaps: past it, points are merged into this many
// time slots (MRTG-style: mean of avg, max of max) before gap splitting.
var MAX_RENDER_POINTS = 1500

// Any watt/amp figure outside this is treated as unreadable rather than drawn.
var MAX_ABS_VALUE = 100000

var RANGES = [
  { key: "1h", label: "1h", tier: "raw", file: "history-raw.jsonl", span: 3600, step: 0 },
  { key: "24h", label: "24h", tier: "1m", file: "history-1m.jsonl", span: 86400, step: 60 },
  { key: "7d", label: "7d", tier: "15m", file: "history-15m.jsonl", span: 604800, step: 900 }
]

// ------------------------------------------------------------ small helpers

// A finite number inside [lo, hi], or null. Strings are not coerced: the
// contract types every number as a JSON number.
function finiteIn(value, lo, hi) {
  if (typeof value !== "number" || !isFinite(value)) return null
  if (value < lo || value > hi) return null
  return value
}

function watts(value) {
  return finiteIn(value, -MAX_ABS_VALUE, MAX_ABS_VALUE)
}

// File-sourced text for the UI: C0/C1 control characters, bidi overrides and
// isolates, zero-width and line/paragraph separators, and the markup trio
// (<, >, &) removed, length capped. Every shown Text is PlainText anyway; this
// is for strings handed to shell components that do not declare a format, and
// so that a crafted value cannot reorder the text around it.
var UNSAFE_LABEL_CHARS = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff<>&]/g

function safeLabel(value, maxLen) {
  if (typeof value !== "string") return ""
  var cap = maxLen > 0 ? maxLen : 40
  var out = value.length > cap * 4 ? value.substring(0, cap * 4) : value
  out = out.replace(UNSAFE_LABEL_CHARS, "").trim()
  return out.length > cap ? out.substring(0, cap) : out
}

// A Canvas font string. The family comes from shell config and is quoted in
// the string, so a `"` or `\` in it would end the quote early and make Canvas
// fall back to its default font: those (and control characters) are dropped.
function canvasFont(pixelSize, family) {
  var px = finiteIn(pixelSize, 1, 512)
  var size = (px === null ? 10 : Math.round(px)) + "px "
  var fam = typeof family === "string" ? family.replace(/["\\\u0000-\u001f\u007f]/g, "").trim() : ""
  return fam === "" ? size + "sans-serif" : size + "\"" + fam + "\""
}

// `value` when it is one of `allowed`, else "".
function oneOf(value, allowed) {
  return typeof value === "string" && allowed.indexOf(value) >= 0 ? value : ""
}

// A PCI address in the service's normalised dddd:bb:dd.f form, else "".
function pciAddress(value) {
  return typeof value === "string" && /^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$/.test(value) ? value : ""
}

// gpu_devices -> { devices, total, partial }: at most MAX_GPU_DEVICES
// sanitised entries, the number of device entries seen, and whether the scan
// stopped at MAX_GPU_DEVICE_SCAN before the end (total is then a floor).
// Entries that are not objects are dropped, unknown fields become "" / null.
function parseGpuDevices(value) {
  var out = { devices: [], total: 0, partial: false }
  if (!Array.isArray(value)) return out
  var scan = Math.min(value.length, MAX_GPU_DEVICE_SCAN)
  out.partial = value.length > scan
  for (var i = 0; i < scan; i++) {
    var d = value[i]
    if (!d || typeof d !== "object" || Array.isArray(d)) continue
    out.total++
    if (out.devices.length >= MAX_GPU_DEVICES) continue
    out.devices.push({
      pci: pciAddress(d.pci),
      vendor: oneOf(d.vendor, GPU_VENDORS),
      status: oneOf(d.status, GPU_DEVICE_STATUSES),
      powerW: watts(d.power_w)
    })
  }
  return out
}

function rangeFor(key) {
  for (var i = 0; i < RANGES.length; i++) if (RANGES[i].key === key) return RANGES[i]
  return RANGES[0]
}

function stepFor(rangeKey, intervalS) {
  var range = rangeFor(rangeKey)
  if (range.step > 0) return range.step
  var interval = finiteIn(intervalS, 0.05, 3600)
  return interval === null ? DEFAULT_INTERVAL_S : interval
}

function stripTrailingSlashes(path) {
  var out = path.replace(/\/+$/, "")
  return out === "" && path !== "" ? "/" : out
}

// Mirrors bin/power-watch state_dir(): $POWER_WATCH_STATE_DIR when set and
// non-empty, else $XDG_STATE_HOME (or ~/.local/state) + /power-watch. No
// trimming, as in Python; trailing slashes go, as pathlib drops them.
function stateDir(envDir, xdgStateHome, home) {
  if (typeof envDir === "string" && envDir !== "") return stripTrailingSlashes(envDir)
  var base = typeof xdgStateHome === "string" && xdgStateHome !== ""
    ? stripTrailingSlashes(xdgStateHome) : stripTrailingSlashes(String(home || "")) + "/.local/state"
  return (base === "/" ? "" : base) + "/power-watch"
}

// A file:// URL for a local directory, every segment percent-encoded so a
// space, "#" or "%" in the path cannot turn into a fragment or an escape.
function fileUrl(path) {
  return "file://" + String(path).split("/").map(encodeURIComponent).join("/")
}

function historyPath(dir, rangeKey) {
  return dir + "/" + rangeFor(rangeKey).file
}

// ------------------------------------------------------------- current.json

// Parses current.json text into a normalized object, or { ok: false, error }.
// "missing" (empty text) is distinguishable from "unreadable".
function parseCurrent(text) {
  var raw = typeof text === "string" ? text : ""
  if (raw.trim() === "") return { ok: false, error: "missing" }
  if (raw.length > CURRENT_MAX_CHARS) return { ok: false, error: "too large" }
  var obj
  try {
    obj = JSON.parse(raw)
  } catch (e) {
    return { ok: false, error: "unreadable" }
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return { ok: false, error: "unreadable" }

  var interval = finiteIn(obj.interval_s, 0.05, 3600)
  var threshold = finiteIn(obj.threshold_w, 1, MAX_ABS_VALUE)
  var kind = obj.total_kind === "measured" || obj.total_kind === "estimated" ? obj.total_kind : ""
  var total = watts(obj.total_w)
  var source = oneOf(obj.total_source, TOTAL_SOURCES)
  var errors = Array.isArray(obj.errors) ? obj.errors.length : 0
  var gpus = parseGpuDevices(obj.gpu_devices)
  var adapterV = finiteIn(obj.adapter_v, 0.1, 1000)
  var ratingW = finiteIn(obj.adapter_rating_w, 1, MAX_ABS_VALUE)

  return {
    ok: true,
    data: {
      epoch: finiteIn(obj.epoch, 0, 1e11),
      intervalS: interval === null ? DEFAULT_INTERVAL_S : interval,
      acOnline: typeof obj.ac_online === "boolean" ? obj.ac_online : null,
      batteryStatus: safeLabel(obj.battery_status, 24),
      batteryCapacity: finiteIn(obj.battery_capacity, 0, 100),
      batteryW: watts(obj.battery_w),
      cpuW: watts(obj.cpu_w),
      gpuW: watts(obj.gpu_w),
      restW: watts(obj.rest_w),
      restSource: safeLabel(obj.rest_source, 24),
      chargeW: watts(obj.charge_w),
      totalW: total,
      totalKind: kind,
      incomplete: obj.incomplete === true,
      // Contract §3: only an estimate (the model, or a service that does not
      // say its source) with a number is a lower bound. A measured total never
      // is, whatever the flag says.
      totalLowerBound: obj.total_lower_bound === true && total !== null && kind === "estimated"
        && (source === "model" || source === ""),
      totalSource: source,
      psysW: watts(obj.psys_w),
      // null = unknown (not configured): no amps, no share of the rating. A
      // value that is there but out of bounds is invalid, said apart from that.
      adapterV: adapterV,
      adapterVInvalid: adapterV === null && obj.adapter_v !== null && obj.adapter_v !== undefined,
      adapterA: finiteIn(obj.adapter_current_a, -1000, 1000),
      adapterRatingW: ratingW,
      adapterRatingInvalid: ratingW === null && obj.adapter_rating_w !== null && obj.adapter_rating_w !== undefined,
      thresholdW: threshold === null ? DEFAULT_THRESHOLD_W : threshold,
      cpuLimitW: finiteIn(obj.cpu_limit_w, 0, MAX_ABS_VALUE),
      gpuLimitW: finiteIn(obj.gpu_limit_w, 0, MAX_ABS_VALUE),
      gpuMaxLimitW: finiteIn(obj.gpu_max_limit_w, 0, MAX_ABS_VALUE),
      gpuPstate: safeLabel(obj.gpu_pstate, 8),
      gpuStatus: oneOf(obj.gpu_status, GPU_STATUSES),
      gpuDevices: gpus.devices,
      gpuDeviceTotal: gpus.total,
      gpuDevicesPartial: gpus.partial,
      errorCount: errors
    }
  }
}

// Stale = no data, no epoch, or an epoch older than STALE_INTERVALS samples —
// or reads, below one second: reads are at most one per CURRENT_READ_MIN_MS,
// so a sub-second interval counts as that long. An epoch that far in the
// future is equally untrustworthy.
function isStale(current, nowEpoch) {
  if (!current || current.epoch === null || current.epoch === undefined) return true
  var interval = current.intervalS > 0 ? current.intervalS : DEFAULT_INTERVAL_S
  var limit = STALE_INTERVALS * Math.max(interval, CURRENT_READ_MIN_MS / 1000)
  var age = nowEpoch - current.epoch
  return !(age <= limit && age >= -limit)
}

// Whether the staleness tick should re-list the state directory. Only when
// current.json was absent: the directory watch never arms on a directory that
// did not exist when it was set (measured under a scratch qs), and re-listing
// re-arms it. A file that is present — old, oversized or unreadable — is
// watched already, and its next replacement arrives through that watch; it is
// never read again on a timer.
function shouldRelist(outcome) {
  return outcome === "missing"
}

// The read gate, from one directory-listing entry ({ size, modified, isDir }
// or null). Returns { action } — "missing", "unreadable", "too large",
// "none" (this exact version was already handed to the reader) or "read",
// with the version string to remember.
// A size of 0 is never read: an empty file has nothing to read, and a file
// whose size the kernel does not know — /proc (kallsyms, self/pagemap) lists
// as 0 bytes and reads as megabytes to gigabytes — would pass any cap and be
// read whole. Both read as "missing", which an empty current.json already
// parsed to and an empty history tier already meant.
function gateDecision(entry, maxBytes, lastVersion) {
  if (!entry) return { action: "missing" }
  if (entry.isDir === true) return { action: "unreadable" }
  var size = entry.size
  if (typeof size !== "number" || !isFinite(size) || size < 0) return { action: "unreadable" }
  if (size === 0) return { action: "missing" }
  if (!(maxBytes > 0) || size > maxBytes) return { action: "too large" }
  var version = size + ":" + (typeof entry.modified === "number" && isFinite(entry.modified) ? entry.modified : "")
  if (version === lastVersion) return { action: "none" }
  return { action: "read", version: version }
}

// A FileView loadFailed: no such file is "missing", anything else (permission
// denied, not a regular file, an I/O error) is "unreadable".
function loadFailure(isFileNotFound) {
  return isFileNotFound ? "missing" : "unreadable"
}

// normal / approaching / over / unknown, against threshold_w.
function level(totalW, thresholdW) {
  if (typeof totalW !== "number" || !isFinite(totalW)) return "unknown"
  var threshold = typeof thresholdW === "number" && isFinite(thresholdW) && thresholdW > 0
    ? thresholdW : DEFAULT_THRESHOLD_W
  if (totalW >= threshold) return "over"
  if (totalW >= threshold * APPROACH_FRACTION) return "approaching"
  return "normal"
}

// ---------------------------------------------------------------- formatting

function formatNumber(value, digits) {
  if (typeof value !== "number" || !isFinite(value)) return "--"
  var d = digits > 0 ? digits : 0
  var out = value.toFixed(d)
  // -0 and "-0.0" read as a sign error.
  if (Number(out) === 0) out = (0).toFixed(d)
  return out
}

function formatWatts(value, digits) {
  return formatNumber(value, digits) + " W"
}

function formatAmps(value) {
  return formatNumber(value, 1) + " A"
}

function formatPercent(part, whole) {
  if (typeof part !== "number" || !isFinite(part) || typeof whole !== "number" || !isFinite(whole) || whole <= 0)
    return "--"
  return Math.round(part / whole * 100) + "%"
}

// Whether a fresh sample's total is a lower bound (shown with LOWER_BOUND_MARK).
function isLowerBound(current) {
  return !!current && current.totalLowerBound === true && typeof current.totalW === "number"
}

// `text` marked as a lower bound when the sample's total is one and `value`
// is a number: figures derived from a lower-bound total are lower bounds too.
function boundText(current, value, text) {
  return isLowerBound(current) && typeof value === "number" && isFinite(value) ? LOWER_BOUND_MARK + text : text
}

function barText(current, stale) {
  if (stale || !current) return "-- W"
  return boundText(current, current.totalW, formatWatts(current.totalW))
}

// The vertical bar shows the number and the unit on two lines, and a lower
// bound's mark on a line of its own above them, so the number keeps its size.
// At most BAR_VERTICAL_SLOTS lines; the bar always reserves that many.
function barLines(current, stale) {
  var text = barText(current, stale)
  var lines = []
  if (text.indexOf(LOWER_BOUND_MARK) === 0) {
    lines.push(LOWER_BOUND_MARK)
    text = text.substring(LOWER_BOUND_MARK.length)
  }
  var cut = text.lastIndexOf(" ")
  if (cut > 0) lines.push(text.substring(0, cut), text.substring(cut + 1))
  else lines.push(text)
  return lines
}

// The CURRENT stat under the chart, marked like the bar.
function currentStatText(rangeStats, current, stale) {
  var value = rangeStats ? rangeStats.current : null
  var text = formatWatts(value)
  return stale ? text : boundText(current, value, text)
}

function powerSourceLabel(acOnline) {
  if (acOnline === true) return "AC"
  if (acOnline === false) return "Battery"
  return "Unknown"
}

// The level in words, so it is not carried by colour alone.
function levelText(lvl, thresholdW) {
  var limit = formatWatts(typeof thresholdW === "number" && thresholdW > 0 ? thresholdW : DEFAULT_THRESHOLD_W)
  if (lvl === "over") return "Over the " + limit + " threshold"
  if (lvl === "approaching") return "Approaching the " + limit + " threshold (" + Math.round(APPROACH_FRACTION * 100) + "%+)"
  if (lvl === "normal") return "Below the " + limit + " threshold"
  return ""
}

// The panel's status line for a fresh sample.
function statusLine(current) {
  var c = current || {}
  var words = levelText(level(c.totalW, c.thresholdW), c.thresholdW)
  return totalLabel(c) + " · " + powerSourceLabel(c.acOnline) + (words ? " · " + words : "")
}

// The rest-of-system source, worded as `power-watch` prints it.
function restSourceLabel(source) {
  if (!source) return ""
  if (source === "default") return "DEFAULT — run power-watch calibrate"
  return source + " calibration"
}

function kindLabel(totalKind) {
  if (totalKind === "measured") return "Measured"
  if (totalKind === "estimated") return "Estimated"
  return "Unknown"
}

// How the total was obtained, short (status line, tooltip). A psys total is
// measured on AC: platform power (PSYS_LABEL), not the battery and not the
// model.
function totalLabel(current) {
  var c = current || {}
  if (c.totalSource === "psys") return PSYS_LABEL
  if (isLowerBound(c)) return "Estimated (lower bound)"
  return kindLabel(c.totalKind)
}

// The breakdown's "Total is" row: the same words, plus "(incomplete)".
function totalRowValue(current) {
  var c = current || {}
  if (isLowerBound(c)) return totalLabel(c)
  return totalLabel(c) + (c.incomplete ? " (incomplete)" : "")
}

var SERVICE_HINT = "systemctl --user enable --now power-watch.service"

// Why there is no fresh number. `error` is the read outcome: a file that
// exists but cannot be read is said apart from no file at all.
function staleMessage(current, error) {
  if (error === "too large")
    return "current.json is too large to read (over " + Math.round(CURRENT_MAX_BYTES / 1024) + " KiB); not read."
  if (error === "unreadable") return "current.json is unreadable."
  return current ? "The power-watch service is not running (last sample is old)."
    : "The power-watch service is not running (no current.json)."
}

function tooltipText(current, stale, error) {
  if (stale || !current) return staleMessage(current, error) + "\nStart it: " + SERVICE_HINT
  var lines = []
  var total = boundText(current, current.totalW, formatWatts(current.totalW))
  // Amps only when the service knows them (it needs the adapter voltage).
  var head = "Adapter " + total + (typeof current.adapterA === "number"
    ? " · " + boundText(current, current.adapterA, formatAmps(current.adapterA)) : "")
  if (current.acOnline === false) head = "Battery " + total
  lines.push(head)
  lines.push(totalLabel(current) + " · " + powerSourceLabel(current.acOnline)
    + (current.incomplete && !isLowerBound(current) ? " · incomplete" : ""))
  var words = levelText(level(current.totalW, current.thresholdW), current.thresholdW)
  if (words) lines.push(words)
  lines.push("Click for history")
  return lines.join("\n")
}

// Two groups, laid out as two columns (BarWidget flows the grid top to
// bottom): what draws the power, then where it comes from.
var BREAKDOWN_ROWS_PER_COLUMN = 5

// The GPU states in words, shared by the GPU row and the GPU limit.
var GPU_STATUS_WORDS = { suspended: "Suspended", none: "No discrete GPU", disabled: "Not monitored" }

// Whether the kept device list is not the whole of gpu_devices: the service
// listed more than MAX_GPU_DEVICES, or more than the scan looked at.
function gpuListCut(current) {
  var c = current || {}
  var n = Array.isArray(c.gpuDevices) ? c.gpuDevices.length : 0
  return c.gpuDevicesPartial === true || (typeof c.gpuDeviceTotal === "number" && c.gpuDeviceTotal > n)
}

// The device count for the GPU row: "3 GPUs", or "16 of 20 GPUs listed" for a
// cut list ("16 of 256+ GPUs listed" past the scan). "" for a single device.
function gpuCountText(current) {
  var c = current || {}
  var n = Array.isArray(c.gpuDevices) ? c.gpuDevices.length : 0
  if (gpuListCut(c)) {
    var total = typeof c.gpuDeviceTotal === "number" && c.gpuDeviceTotal > n ? c.gpuDeviceTotal : n
    return n + " of " + total + (c.gpuDevicesPartial === true ? "+" : "") + " GPUs listed"
  }
  return n > 1 ? n + " GPUs" : ""
}

// The GPU row: watts for an active GPU, the state in words otherwise (a
// suspended or absent GPU is not "0 W"). gpu_status "" is a service that does
// not report it: watts, as before. The P-state belongs to one GPU, so it is
// left out next to the watts of several active ones (or of a cut list).
function gpuRowValue(current) {
  var c = current || {}
  var devices = Array.isArray(c.gpuDevices) ? c.gpuDevices : []
  var n = devices.length
  var count = gpuCountText(c)
  var cut = gpuListCut(c)
  if (c.gpuStatus === "suspended") return "Suspended" + (count ? " (" + count + ")" : "")
  if (GPU_STATUS_WORDS[c.gpuStatus]) return GPU_STATUS_WORDS[c.gpuStatus]
  if (c.gpuStatus === "unknown")
    return typeof c.gpuW === "number" ? formatWatts(c.gpuW) + " + unknown" : "Unknown"
  var active = 0
  for (var i = 0; i < n; i++) if (devices[i].status === "active") active++
  var extras = []
  if (c.gpuPstate && active <= 1 && !cut) extras.push(c.gpuPstate)
  if (cut) extras.push(count)
  else if (n > 1) extras.push(active > 0 && active < n ? active + " of " + n + " active" : count)
  return formatWatts(c.gpuW) + (extras.length ? " (" + extras.join(", ") + ")" : "")
}

function breakdown(current) {
  var c = current || {}
  var sampled = !!current
  var charging = typeof c.chargeW === "number" && c.chargeW > 0
  var battery = c.batteryStatus || "Unknown"
  if (typeof c.batteryCapacity === "number") battery += ", " + Math.round(c.batteryCapacity) + "%"
  var rest = restSourceLabel(c.restSource)
  // A sample that reports no voltage (or rating) has none configured: say so
  // instead of a blank figure; one out of bounds is invalid, not unset. No
  // sample at all keeps the "--" placeholders.
  var amps = sampled && typeof c.adapterV !== "number" ? (c.adapterVInvalid === true ? "Invalid" : "Not configured")
    : boundText(c, c.adapterA, formatAmps(c.adapterA))
  var rating = sampled && typeof c.adapterRatingW !== "number"
    ? (c.adapterRatingInvalid === true ? "Invalid" : "Not configured")
    : boundText(c, typeof c.adapterRatingW === "number" ? c.totalW : null, formatPercent(c.totalW, c.adapterRatingW))
      + (typeof c.adapterRatingW === "number" ? " of " + formatWatts(c.adapterRatingW) : "")
  return [
    { label: "CPU", value: formatWatts(c.cpuW) },
    { label: "GPU", value: gpuRowValue(c) },
    { label: "Rest", value: formatWatts(c.restW) + (rest ? " (" + rest + ")" : "") },
    { label: "Charging", value: charging ? formatWatts(c.chargeW) : "No" },
    { label: "Total is", value: totalRowValue(c) },
    { label: "Power source", value: powerSourceLabel(c.acOnline) },
    { label: "Battery", value: battery },
    { label: "Adapter current", value: amps },
    { label: "Of adapter rating", value: rating }
  ]
}

// The section title over the breakdown: a stale sample is not "now".
function sampleTitle(current, stale) {
  if (!current) return "NO SAMPLE"
  return stale ? "LAST SAMPLE" : "NOW"
}

// Warnings under a fresh sample, worded as `power-watch` prints them.
function sampleNotes(current) {
  if (!current) return ""
  var notes = []
  if (isLowerBound(current)) notes.push("Lower bound: unknown terms counted as 0 W.")
  else if (current.incomplete) notes.push("Estimate incomplete: unknown terms counted as 0 W.")
  if (current.restSource === "default") notes.push("Rest: " + restSourceLabel("default") + ".")
  if (current.errorCount > 0) notes.push(current.errorCount + " sensor error(s); see power-watch --once.")
  return notes.join(" ")
}

// CPU PL1, and the GPU limit against its maximum. Without a reported maximum
// the limit is shown as is: whether it is capped cannot be told. Without a
// limit, a GPU that is suspended, absent or off says so.
function capStatus(current) {
  var c = current || {}
  var cpu = typeof c.cpuLimitW === "number" ? "PL1 " + formatWatts(c.cpuLimitW) : "Unknown"
  var gpu = GPU_STATUS_WORDS[c.gpuStatus] || "Unknown"
  var gpuCapped = false
  if (typeof c.gpuLimitW === "number") {
    var max = typeof c.gpuMaxLimitW === "number" && c.gpuMaxLimitW > 0 ? c.gpuMaxLimitW : null
    gpuCapped = max !== null && c.gpuLimitW < max - 0.5
    gpu = max === null ? formatWatts(c.gpuLimitW) + " (max unknown)"
      : gpuCapped ? formatWatts(c.gpuLimitW) + " of " + formatWatts(max) + " (capped)"
      : formatWatts(c.gpuLimitW) + " (max, uncapped)"
  }
  return { cpu: cpu, gpu: gpu, gpuCapped: gpuCapped }
}

// --------------------------------------------------------------- history

function normalizePoint(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null
  var t = finiteIn(obj.t, 0, 1e11)
  if (t === null) return null
  var n = finiteIn(obj.n, 1, 1e7)
  var ac = finiteIn(obj.ac, 0, 1)
  var k = obj.k === "m" || obj.k === "e" || obj.k === "x" ? obj.k : ""
  return {
    t: t,
    n: n === null ? 1 : Math.floor(n),
    avg: watts(obj.avg),
    max: watts(obj.max),
    cpu: watts(obj.cpu),
    gpu: watts(obj.gpu),
    rest: watts(obj.rest),
    chg: watts(obj.chg),
    ac: ac,
    k: k
  }
}

// JSONL text -> { points, skipped, truncated }. Parsed from the END of the
// text: at most the newest HISTORY_PARSE_CHARS characters (cut at a line
// start) and the newest HISTORY_MAX_LINES lines, so a cap drops the OLDEST
// points; `truncated` says one applied. The backward line count is bounded by
// the line cap, the forward parse by the character budget.
// Tolerant: blank lines are ignored; torn or garbage lines and lines whose t
// does not advance are skipped and counted. Points stamped after `nowEpoch`
// (a clock that went backwards) are ignored, uncounted, and do not hold back
// the points after them.
function parseHistory(text, nowEpoch) {
  var raw = typeof text === "string" ? text : ""
  var start = 0
  var truncated = false
  if (raw.length > HISTORY_PARSE_CHARS) {
    var nl = raw.indexOf("\n", raw.length - HISTORY_PARSE_CHARS - 1)
    start = nl < 0 ? raw.length : nl + 1
    truncated = true
  }
  var p = raw.length
  if (p > start && raw.charCodeAt(p - 1) === 10) p--
  var counted = 0
  while (p > start) {
    var back = raw.lastIndexOf("\n", p - 1)
    if (back < start) break
    counted++
    if (counted >= HISTORY_MAX_LINES) {
      start = back + 1
      truncated = true
      break
    }
    p = back
  }
  var limit = typeof nowEpoch === "number" && isFinite(nowEpoch) ? nowEpoch : Infinity
  var points = []
  var skipped = 0
  var lastT = -Infinity
  while (start < raw.length) {
    var end = raw.indexOf("\n", start)
    if (end < 0) end = raw.length
    var line = raw.substring(start, end)
    start = end + 1
    if (line.trim() === "") continue
    if (line.length > HISTORY_MAX_LINE_CHARS) { skipped++; continue }
    var obj = null
    try { obj = JSON.parse(line) } catch (e) { obj = null }
    var point = normalizePoint(obj)
    if (point && point.t > limit) continue
    if (!point || point.t <= lastT) { skipped++; continue }
    lastT = point.t
    points.push(point)
  }
  return { points: points, skipped: skipped, truncated: truncated }
}

function filterRange(points, fromEpoch, toEpoch) {
  var out = []
  var list = points || []
  for (var i = 0; i < list.length; i++) {
    var t = list[i].t
    if (t >= fromEpoch && t <= toEpoch) out.push(list[i])
  }
  return out
}

// The last sample time a point stands for: a merged chart point covers
// t..tEnd, a file point only its own t.
function endOf(point) {
  return typeof point.tEnd === "number" ? point.tEnd : point.t
}

// Splits a sorted point list into gap-free runs.
function splitGaps(points, step) {
  var runs = []
  var list = points || []
  var limit = GAP_STEPS * (step > 0 ? step : DEFAULT_INTERVAL_S)
  var run = []
  for (var i = 0; i < list.length; i++) {
    if (run.length > 0 && list[i].t - endOf(run[run.length - 1]) > limit) {
      runs.push(run)
      run = []
    }
    run.push(list[i])
  }
  if (run.length > 0) runs.push(run)
  return runs
}

// battery (measured), ac (estimated), acMeasured (AC, measured as RAPL psys),
// mixed, or unknown. k = "m" is "every sample measured", which is the battery
// on battery and psys on AC: `ac` (the share of samples on AC) tells them
// apart. The service measures the battery only when AC is known to be off, so
// a measured bucket with no AC state is psys.
function pointMode(point) {
  if (!point) return "unknown"
  // A merged chart point carries the mode its points agreed on (mergePoints).
  if (typeof point.mode === "string") return point.mode
  if (point.ac === 0) return "battery"
  if (point.k === "m") return point.ac === 1 || point.ac === null ? "acMeasured" : "mixed"
  if (point.k === "e") return "ac"
  if (point.k === "x") return "mixed"
  if (point.ac === 1) return "ac"
  if (typeof point.ac === "number") return "mixed"
  return "unknown"
}

// Whether the legend needs the PSYS_BAND_LABEL band: the chart shows one, or
// the current sample is measured that way.
function showsMeasuredAc(chart, current) {
  if (current && current.totalSource === "psys") return true
  var spans = chart && Array.isArray(chart.spans) ? chart.spans : []
  for (var i = 0; i < spans.length; i++) if (spans[i].mode === "acMeasured") return true
  return false
}

// Runs of one power mode as [t0, t1) spans; a gap ends a span as well.
function modeSpans(points, step) {
  var spans = []
  var s = step > 0 ? step : DEFAULT_INTERVAL_S
  var runs = splitGaps(points, s)
  for (var r = 0; r < runs.length; r++) {
    var run = runs[r]
    var span = null
    for (var i = 0; i < run.length; i++) {
      var mode = pointMode(run[i])
      if (span && span.mode === mode) {
        span.t1 = endOf(run[i]) + s
        continue
      }
      if (span) {
        span.t1 = run[i].t
        spans.push(span)
      }
      span = { mode: mode, t0: run[i].t, t1: endOf(run[i]) + s }
    }
    if (span) spans.push(span)
  }
  return spans
}

// Max / Average / Current for the range: max of max, n-weighted mean of avg,
// and current.json's total while it is fresh (null otherwise: a stale number is
// not "current").
function stats(points, current, stale) {
  var max = null
  var sum = 0
  var weight = 0
  var list = points || []
  for (var i = 0; i < list.length; i++) {
    var p = list[i]
    var peak = p.max !== null ? p.max : p.avg
    if (peak !== null && (max === null || peak > max)) max = peak
    if (p.avg !== null) {
      sum += p.avg * p.n
      weight += p.n
    }
  }
  return {
    max: max,
    avg: weight > 0 ? sum / weight : null,
    current: !stale && current && typeof current.totalW === "number" ? current.totalW : null
  }
}

// n-weighted mean of list[from..to) over non-null values of key.
function meanOf(list, from, to, key) {
  var sum = 0
  var weight = 0
  for (var i = from; i < to; i++) {
    var v = list[i][key]
    if (v === null || v === undefined) continue
    sum += v * list[i].n
    weight += list[i].n
  }
  return weight > 0 ? sum / weight : null
}

// list[from..to) as one point: n summed, max of max (or of avg where a point
// has no max), n-weighted means, the k shared by all or "x" (mixed). It
// covers t..tEnd, so gap detection still sees the real spacing.
// Its band `mode` is the one its points share, else "mixed": decided from the
// points, not from the merged fields, because the mean of `ac` skips the
// points with none — psys points with no AC state (acMeasured) merged with
// battery ones (ac 0) would otherwise average to ac 0 and read as battery.
function mergePoints(list, from, to) {
  if (to - from === 1) return list[from]
  var n = 0
  var max = null
  var k = list[from].k
  var mode = pointMode(list[from])
  for (var i = from; i < to; i++) {
    n += list[i].n
    var peak = list[i].max !== null ? list[i].max : list[i].avg
    if (peak !== null && (max === null || peak > max)) max = peak
    if (list[i].k !== k) k = "x"
    if (mode !== "mixed" && pointMode(list[i]) !== mode) mode = "mixed"
  }
  return {
    t: list[from].t, tEnd: endOf(list[to - 1]), n: n,
    avg: meanOf(list, from, to, "avg"), max: max,
    cpu: meanOf(list, from, to, "cpu"), gpu: meanOf(list, from, to, "gpu"),
    rest: meanOf(list, from, to, "rest"), chg: meanOf(list, from, to, "chg"),
    ac: meanOf(list, from, to, "ac"), k: k, mode: mode
  }
}

// Merges a sorted point list into time slots `slot` seconds wide starting at
// `from`: one point per non-empty slot, in one linear pass. A gap that falls
// inside one slot is narrower than the chart can show and merges away; every
// wider gap survives, because the merged points keep their t..tEnd.
function consolidate(points, fromEpoch, slot) {
  var list = points || []
  if (!(slot > 0)) return list
  var out = []
  var i = 0
  while (i < list.length) {
    var key = Math.floor((list[i].t - fromEpoch) / slot)
    var j = i + 1
    while (j < list.length && Math.floor((list[j].t - fromEpoch) / slot) === key) j++
    out.push(mergePoints(list, i, j))
    i = j
  }
  return out
}

// "Nice" axis ceiling and step for 0..value, ~4 divisions.
function niceAxis(maxValue) {
  var top = typeof maxValue === "number" && isFinite(maxValue) && maxValue > 0 ? maxValue : 1
  var steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000]
  var step = steps[steps.length - 1]
  for (var i = 0; i < steps.length; i++) {
    if (top / steps[i] <= 5) { step = steps[i]; break }
  }
  var axisMax = Math.ceil(top / step) * step
  if (axisMax <= top) axisMax += step
  var ticks = []
  for (var v = 0; v <= axisMax + 1e-9; v += step) ticks.push(v)
  return { max: axisMax, step: step, ticks: ticks }
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

// Local-time tick marks for the time axis.
function timeTicks(fromEpoch, toEpoch, rangeKey) {
  var ticks = []
  if (!(toEpoch > fromEpoch)) return ticks
  var d = new Date(fromEpoch * 1000)
  var stepHours = 0
  var stepMinutes = 0
  if (rangeKey === "7d") {
    d.setHours(0, 0, 0, 0)
  } else if (rangeKey === "24h") {
    stepHours = 3
    d.setMinutes(0, 0, 0)
    d.setHours(d.getHours() - d.getHours() % stepHours)
  } else {
    stepMinutes = 10
    d.setSeconds(0, 0)
    d.setMinutes(d.getMinutes() - d.getMinutes() % stepMinutes)
  }
  for (var guard = 0; guard < 64; guard++) {
    var t = d.getTime() / 1000
    if (t > toEpoch) break
    if (t >= fromEpoch) {
      var label
      if (rangeKey === "7d") label = WEEKDAYS[d.getDay()] + " " + d.getDate()
      else label = pad2(d.getHours()) + ":" + pad2(d.getMinutes())
      ticks.push({ t: t, label: label })
    }
    if (rangeKey === "7d") d.setDate(d.getDate() + 1)
    else if (rangeKey === "24h") d.setHours(d.getHours() + stepHours)
    else d.setMinutes(d.getMinutes() + stepMinutes)
  }
  return ticks
}

// Everything the chart needs, in data space, for one range.
function chartModel(points, rangeKey, nowEpoch, intervalS, thresholdW) {
  var range = rangeFor(rangeKey)
  var step = stepFor(rangeKey, intervalS)
  var to = nowEpoch
  var from = nowEpoch - range.span
  var visible = filterRange(points, from, to)
  // The cap is on the whole range: past it, MAX_RENDER_POINTS slots cover
  // [from, to] (a point at `to` falls in the last one), so drawn points, runs
  // and mode spans are each at most the cap however the gaps fall.
  var slot = visible.length > MAX_RENDER_POINTS ? range.span / (MAX_RENDER_POINTS - 1) : 0
  var merged = consolidate(visible, from, slot)
  var drawn = splitGaps(merged, step)
  var top = 0
  for (var r = 0; r < drawn.length; r++) {
    var run = drawn[r]
    for (var i = 0; i < run.length; i++) {
      var p = run[i]
      var peak = Math.max(p.max === null ? 0 : p.max, p.avg === null ? 0 : p.avg,
        p.cpu === null ? 0 : p.cpu, p.gpu === null ? 0 : p.gpu)
      if (peak > top) top = peak
    }
  }
  var threshold = typeof thresholdW === "number" && thresholdW > 0 ? thresholdW : DEFAULT_THRESHOLD_W
  return {
    range: range.key,
    from: from,
    to: to,
    step: step,
    // Buckets are stamped with their start; plot them at their middle.
    offset: range.step > 0 ? step / 2 : 0,
    runs: drawn,
    spans: modeSpans(merged, step),
    count: visible.length,
    threshold: threshold,
    axis: niceAxis(Math.max(top, threshold) * 1.05),
    ticks: timeTicks(from, to, range.key)
  }
}

// --------------------------------------------------------------- geometry

// How the chart draws each series, shared with the legend so the two cannot
// drift. Widths and dashes are unscaled px (both go through Style.spaceReal).
// The series differ in pattern and weight, not only in colour, because the
// theme's accent equals its foreground in some themes: Max is a thin dotted
// line, CPU a thick solid one, GPU a thick long-dashed one.
var CHART_STYLE = {
  areaAlpha: 0.26, areaLineAlpha: 0.45,
  maxAlpha: 0.9, maxWidth: 1, maxDash: [2, 2],
  cpuWidth: 2,
  gpuAlpha: 0.75, gpuWidth: 2, gpuDash: [8, 4],
  thresholdDash: [6, 4],
  batteryAlpha: 0.95, mixedAlpha: 0.45, acAlpha: 0.22, acMeasuredAlpha: 0.65, unknownAlpha: 0.08
}

// Data space -> pixel primitives for a plot rectangle. The Canvas only strokes
// and fills what this returns.
function chartLayout(model, width, height, metrics) {
  var m = metrics || {}
  var left = m.left || 48
  var right = m.right || 8
  var top = m.top || 8
  var band = m.band || 6
  var bottom = m.bottom || 20
  var plot = { x: left, y: top, w: Math.max(1, width - left - right), h: Math.max(1, height - top - bottom - band - 4) }
  var out = { plot: plot, bandY: plot.y + plot.h + 3, bandH: band, area: [], max: [], cpu: [], gpu: [],
    spans: [], yTicks: [], xTicks: [], thresholdY: null, empty: true }
  if (!model || !(model.to > model.from)) return out
  var span = model.to - model.from
  var yMax = model.axis.max
  function px(t) { return plot.x + (t - model.from) / span * plot.w }
  function py(v) { return plot.y + plot.h - Math.max(0, Math.min(v, yMax)) / yMax * plot.h }
  var minW = Math.max(1, model.step / span * plot.w)

  function lines(run, key) {
    var polys = []
    var cur = []
    for (var i = 0; i < run.length; i++) {
      var v = run[i][key]
      if (v === null) {
        if (cur.length) polys.push(cur)
        cur = []
        continue
      }
      cur.push([px(run[i].t + model.offset), py(v)])
    }
    if (cur.length) polys.push(cur)
    // A lone sample still deserves a mark one step wide.
    for (var j = 0; j < polys.length; j++) {
      if (polys[j].length === 1) polys[j].push([polys[j][0][0] + minW, polys[j][0][1]])
    }
    return polys
  }

  // push, never concat: concat inside this loop copies the whole output once
  // per run, which is quadratic in the number of gaps.
  function append(dst, src) {
    for (var i = 0; i < src.length; i++) dst.push(src[i])
  }

  for (var r = 0; r < model.runs.length; r++) {
    var run = model.runs[r]
    append(out.area, lines(run, "avg"))
    append(out.max, lines(run, "max"))
    append(out.cpu, lines(run, "cpu"))
    append(out.gpu, lines(run, "gpu"))
  }
  out.empty = model.count === 0
  for (var s = 0; s < model.spans.length; s++) {
    var sp = model.spans[s]
    var x0 = Math.max(plot.x, px(sp.t0))
    var x1 = Math.min(plot.x + plot.w, px(sp.t1))
    if (x1 > x0) out.spans.push({ mode: sp.mode, x: x0, w: Math.max(1, x1 - x0) })
  }
  // The top tick carries the unit.
  var lastTick = model.axis.ticks.length - 1
  for (var y = 0; y <= lastTick; y++) {
    var value = model.axis.ticks[y]
    out.yTicks.push({ y: py(value), label: String(value) + (y === lastTick ? " W" : "") })
  }
  for (var x = 0; x < model.ticks.length; x++) {
    out.xTicks.push({ x: px(model.ticks[x].t), label: model.ticks[x].label })
  }
  out.thresholdY = py(model.threshold)
  out.baseY = plot.y + plot.h
  return out
}

// Centre x for an axis label `labelW` px wide, kept inside the plot so the
// first and last labels are not clipped at its edges.
function clampLabelX(x, labelW, plotX, plotW) {
  var half = Math.max(0, labelW) / 2
  if (plotW <= half * 2) return plotX + plotW / 2
  return Math.max(plotX + half, Math.min(plotX + plotW - half, x))
}

// Most dash segments one series may stroke, across all of its polylines.
var MAX_DASH_SEGMENTS = 20000

// A polyline cut into dash segments [x0, y0, x1, y1]: `on` px drawn, `off` px
// skipped, the pattern carried across vertices. The Canvas strokes these
// instead of relying on setLineDash. At most `budget` segments (default
// MAX_DASH_SEGMENTS).
function dashSegments(poly, on, off, budget) {
  var out = []
  var dash = on > 0 ? on : 4
  var gap = off > 0 ? off : 3
  var cap = budget > 0 ? Math.min(budget, MAX_DASH_SEGMENTS) : MAX_DASH_SEGMENTS
  var drawing = true
  var left = dash
  for (var i = 1; i < (poly ? poly.length : 0); i++) {
    var x0 = poly[i - 1][0], y0 = poly[i - 1][1]
    var dx = poly[i][0] - x0, dy = poly[i][1] - y0
    var len = Math.sqrt(dx * dx + dy * dy)
    var pos = 0
    while (pos < len && out.length < cap) {
      var take = Math.min(left, len - pos)
      if (drawing) {
        out.push([x0 + dx * pos / len, y0 + dy * pos / len, x0 + dx * (pos + take) / len, y0 + dy * (pos + take) / len])
      }
      pos += take
      left -= take
      if (left <= 1e-9) {
        drawing = !drawing
        left = drawing ? dash : gap
      }
    }
  }
  return out
}
