import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Power Watch: the AC adapter load (estimated, or measured where the machine
// can) in the bar, and its history on click.
//
// Data comes from the power-watch service (see the repo README): current.json,
// rewritten by atomic rename after every sample, and three JSON Lines history
// tiers. This file only reads them; all parsing and maths is in Model.js.
//
// Overhead, by design:
//   - No Process, ever. Both files are read with FileView, and only through
//     GatedFile.qml: a directory listing (FolderListModel, Qt's worker thread)
//     says how big the file is, and a file over its cap (Model.CURRENT_MAX_BYTES,
//     HISTORY_MAX_BYTES) is never read at all — it shows as "too large", not
//     as missing.
//   - current.json: the listing watches the state directory, so every atomic
//     rewrite (os.replace) re-lists it and, when the size or mtime moved, reads
//     the new file. Nothing is read while nothing changes. Each bar (one per
//     monitor) has its own reader, so a sample costs N heads x (readdir + stat
//     + <= 16 KiB read + JSON.parse); reads are at most one per
//     Model.CURRENT_READ_MIN_MS (1 s) per bar. Staleness window: the number
//     lags the file by at most that second (at the default 2 s interval, not
//     at all: one sample, one read), and goes stale after 3 x max(interval, 1 s).
//   - One repeating Timer (Model.STALE_CHECK_MS, 10 s): compares the sample's
//     epoch with the clock, and re-lists the directory only while current.json
//     is absent (a directory watch never arms on a directory that did not exist
//     yet). A present file — old, oversized or unreadable — is never re-read on
//     a timer: its next replacement arrives through the watch.
//   - History is read only while the panel is open: its GatedFile lives in a
//     Loader gated on `opened`, so closing the panel drops the listing, the
//     watch and the text. The tiers are appended in place, which a directory
//     watch does not see, so the FileView's own watch reports appends and the
//     history is re-listed and re-read at most once per
//     Model.HISTORY_THROTTLE_MS, which also bounds chart repaints.
//
// Parse caps sit behind the read caps: current.json is not parsed past
// Model.CURRENT_MAX_CHARS, and a history tier is parsed from its end, at most
// Model.HISTORY_PARSE_CHARS / HISTORY_MAX_LINES of it (Model.parseHistory).
//
// Every Text in this file declares textFormat: Text.PlainText; file-sourced
// strings are additionally stripped of markup in Model.safeLabel. The model test
// counts the declarations against the Text items.
Panel {
  id: root
  moduleName: "io.github.rsd.power-watch"
  ipcTarget: "io.github.rsd.power-watch"

  // ------------------------------------------------------------- data state

  readonly property string stateDir: Model.stateDir(Quickshell.env("POWER_WATCH_STATE_DIR"),
    Quickshell.env("XDG_STATE_HOME"), Quickshell.env("HOME"))

  property var current: null
  // The last read outcome for current.json: "" (read fine), "missing",
  // "too large" or "unreadable".
  property string currentError: "missing"
  property real now: Date.now() / 1000
  readonly property bool stale: Model.isStale(root.current, root.now)
  readonly property string level: root.stale || !root.current ? "unknown" : Model.level(root.current.totalW, root.current.thresholdW)

  // Scalars the chart depends on. Properties notify only when the value
  // changes, so a current.json rewrite with the same interval and threshold
  // does not rebuild (or repaint) the chart.
  readonly property real intervalS: root.current ? root.current.intervalS : Model.DEFAULT_INTERVAL_S
  readonly property real thresholdW: root.current ? root.current.thresholdW : Model.DEFAULT_THRESHOLD_W

  // Which range the panel shows; remembered for the life of the shell.
  property string range: Model.RANGES[0].key
  // One object per load, assigned once, so a reload rebuilds the chart once.
  // `at` is when it was read: the chart's right edge. 0 = nothing loaded.
  readonly property var noHistory: ({ points: [], skipped: 0, truncated: false, error: "", at: 0 })
  property var history: root.noHistory
  readonly property string historyError: root.history.error
  readonly property int historySkipped: root.history.skipped

  readonly property var chart: root.opened && root.history.at > 0
    ? Model.chartModel(root.history.points, root.range, root.history.at, root.intervalS, root.thresholdW)
    : null
  readonly property var visiblePoints: root.opened && root.history.at > 0
    ? Model.filterRange(root.history.points, root.history.at - Model.rangeFor(root.range).span, root.history.at)
    : []
  readonly property var rangeStats: Model.stats(root.visiblePoints, root.current, root.stale)
  readonly property var caps: Model.capStatus(root.current)
  readonly property var rows: Model.breakdown(root.current)

  readonly property string historyNote: {
    if (root.historyError !== "") return root.historyError
    var notes = []
    if (root.history.truncated) notes.push("Older points not shown: the history file is past the parse budget.")
    if (root.historySkipped > 0) notes.push(root.historySkipped + " unreadable history line" + (root.historySkipped === 1 ? "" : "s") + " skipped.")
    return notes.join(" ")
  }

  // ----------------------------------------------------------------- style

  readonly property bool vertical: root.bar ? root.bar.vertical === true : false
  readonly property color barColor: root.bar ? root.bar.barForeground : Commons.Color.foreground
  readonly property color foreground: root.bar ? root.bar.foreground : Commons.Color.foreground
  readonly property color urgent: root.bar ? root.bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family

  // The theme has no "warning" role. Approaching is the bar colour tinted
  // halfway toward urgent, so it reads as on the way to the over-threshold
  // colour in every theme (accent alone equals the foreground in some).
  function levelColor(base) {
    if (root.level === "over") return root.urgent
    if (root.level === "approaching") return Qt.tint(base, Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.55))
    return base
  }

  readonly property string barText: Model.barText(root.current, root.stale)
  readonly property var barLines: Model.barLines(root.current, root.stale)

  // --------------------------------------------------------------- actions

  function applyCurrent(text) {
    root.now = Date.now() / 1000
    var parsed = Model.parseCurrent(text)
    if (parsed.ok) {
      root.current = parsed.data
      root.currentError = ""
      return
    }
    root.applyCurrentFailure(parsed.error)
  }

  // A missing file clears the view; an oversized or unreadable one keeps the
  // last good sample, which then ages into the stale state on its own.
  function applyCurrentFailure(outcome) {
    root.now = Date.now() / 1000
    if (outcome === "missing") root.current = null
    root.currentError = outcome
  }

  function applyHistory(text) {
    var at = Date.now() / 1000
    var parsed = Model.parseHistory(text, at)
    root.history = { points: parsed.points, skipped: parsed.skipped, truncated: parsed.truncated, error: "", at: at }
  }

  // No file yet is an empty history, not an error: the service writes the
  // tiers as samples arrive, and the listing picks the file up once it
  // exists. An oversized or unreadable one says so.
  function applyHistoryFailure(outcome) {
    var error = outcome === "too large"
      ? "History file too large to read (over " + Math.round(Model.HISTORY_MAX_BYTES / 1048576) + " MiB); not read."
      : outcome === "unreadable" ? "History file unreadable." : ""
    root.history = { points: [], skipped: 0, truncated: false, error: error, at: Date.now() / 1000 }
  }

  function selectRange(key) {
    var next = Model.rangeFor(key).key
    if (next === root.range) return
    root.history = root.noHistory
    root.range = next
  }

  function stepRange(delta) {
    var index = 0
    for (var i = 0; i < Model.RANGES.length; i++) if (Model.RANGES[i].key === root.range) index = i
    var next = Math.max(0, Math.min(Model.RANGES.length - 1, index + delta))
    root.selectRange(Model.RANGES[next].key)
  }

  onOpenedChanged: {
    if (root.opened) return
    // Closing drops the history text (the Loader) and the parsed points.
    root.history = root.noHistory
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  readonly property real openPanelIndicatorWidth: root.vertical ? 0 : button.labelWidth

  // ------------------------------------------------------------- file reads

  GatedFile {
    id: currentGate
    dir: root.stateDir
    name: "current.json"
    maxBytes: Model.CURRENT_MAX_BYTES
    // At most one read a second per bar; see the header comment.
    minIntervalMs: Model.CURRENT_READ_MIN_MS
    onTextRead: function(content) { root.applyCurrent(content) }
    onFailed: function(outcome) { root.applyCurrentFailure(outcome) }
  }

  // The only repeating timer. Spawns nothing; see the header comment.
  Timer {
    interval: Model.STALE_CHECK_MS
    running: true
    repeat: true
    onTriggered: {
      root.now = Date.now() / 1000
      if (Model.shouldRelist(currentGate.outcome)) currentGate.relist()
      // Same rule for the open panel's tier: absent = possibly no directory
      // to watch yet.
      var reader = historyLoader.item
      if (reader && Model.shouldRelist(reader.outcome)) reader.relist()
    }
  }

  // The open panel's tier. Its GatedFile watches the file for appends and
  // reads it at most once per Model.HISTORY_THROTTLE_MS — whatever triggers
  // the read: an append, or the directory re-listing on every current.json
  // rewrite.
  Loader {
    id: historyLoader
    active: root.opened
    sourceComponent: Component {
      GatedFile {
        dir: root.stateDir
        name: Model.rangeFor(root.range).file
        maxBytes: Model.HISTORY_MAX_BYTES
        watchContent: true
        minIntervalMs: Model.HISTORY_THROTTLE_MS
        onTextRead: function(content) { root.applyHistory(content) }
        onFailed: function(outcome) { root.applyHistoryFailure(outcome) }
      }
    }
  }

  // --------------------------------------------------------------- bar button

  TextMetrics {
    id: widest
    font.family: button.fontFamily
    font.pixelSize: button.fontSize
    // Room for three digits and the lower-bound mark, whether it shows or not.
    text: Model.BAR_WIDEST_TEXT
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "" : root.barText
    labelVisible: !root.vertical
    hasVisualContent: true
    // Wide enough for three digits and the lower-bound mark (horizontal), tall
    // enough for the mark's line, the number and the unit (vertical), always:
    // neither the load moving nor the mark toggling resizes the bar, so its
    // neighbours never shift.
    fixedWidth: root.vertical ? -1 : Math.ceil(widest.advanceWidth + button.scaledHorizontalMargin * 2)
    fixedHeight: root.vertical ? Model.BAR_VERTICAL_SLOTS * Style.bar.iconSlot : -1
    foreground: root.levelColor(root.barColor)
    dimmed: root.stale
    tooltipText: Model.tooltipText(root.current, root.stale, root.currentError)
    onPressed: function(b) { root.toggle() }

    // Without the mark, its slot stays empty above the number, so the digits
    // do not move when it toggles either.
    Column {
      visible: root.vertical
      anchors.fill: parent
      topPadding: Math.max(0, Model.BAR_VERTICAL_SLOTS - root.barLines.length) * Style.bar.iconSlot

      Repeater {
        model: root.barLines

        OpticalGlyph {
          required property string modelData
          width: button.width
          height: Style.bar.iconSlot
          text: modelData
          fontFamily: button.fontFamily
          fontSize: modelData.length > 2 ? button.fontSize * 0.85 : button.fontSize
          color: button.foreground
        }
      }
    }
  }

  // -------------------------------------------------------------------- panel

  // Up/down (and j/k) scroll the panel when it is taller than the screen.
  function scrollBy(dy) {
    var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
    panelFlick.contentY = Math.max(0, Math.min(maxY, panelFlick.contentY + dy * Style.space(48)))
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(540))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (dx !== 0) root.stepRange(dx)
        if (dy !== 0) root.scrollBy(dy)
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      // The content is ~950 px tall at the default scale: on a short screen
      // or with a large font it is taller than fittedContentHeight allows,
      // and scrolls instead of being cut off.
      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------- hero: title + status, big current number ----------
          Item {
            width: parent.width
            implicitHeight: Math.max(heroLabels.implicitHeight, heroValue.implicitHeight)

            Column {
              id: heroLabels
              anchors.left: parent.left
              anchors.right: heroValue.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                textFormat: Text.PlainText
                text: "Power Watch"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }

              // The level in words as well as in colour.
              Text {
                textFormat: Text.PlainText
                width: parent.width
                wrapMode: Text.Wrap
                text: root.stale
                  ? Model.staleMessage(root.current, root.currentError)
                  : Model.statusLine(root.current).toUpperCase()
                color: root.stale ? root.urgent : root.levelColor(root.foreground)
                opacity: root.stale || root.level !== "normal" ? 1 : 0.7
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: !root.stale
                font.letterSpacing: root.stale ? 0 : Style.spaceReal(1.2)
              }

              Text {
                textFormat: Text.PlainText
                visible: root.stale
                width: parent.width
                wrapMode: Text.WrapAnywhere
                text: Model.SERVICE_HINT
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            // Stale: the bar colour at reduced opacity, which recedes on dark
            // and light themes alike (Qt.darker got stronger on light ones).
            Text {
              id: heroValue
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.barText
              color: root.stale ? root.foreground : root.levelColor(root.foreground)
              opacity: root.stale ? 0.45 : 1
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              font.bold: true
            }
          }

          // ---------- range tabs ----------
          ButtonGroup {
            options: Model.RANGES.map(function(r) { return { value: r.key, label: r.label } })
            value: root.range
            focusable: false
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onChanged: function(chosen) { root.selectRange(chosen) }
          }

          // ---------- chart ----------
          Chart {
            id: chartView
            width: parent.width
            height: Style.space(200)
            chart: root.chart
            foreground: root.foreground
            accent: root.accent
            urgent: root.urgent
            labelBackground: Commons.Color.popups.background
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            emptyText: root.historyError !== "" ? root.historyError
              : root.history.at > 0 ? "No history for this range yet" : "Loading history"
          }

          // ---------- legend: the lines, then the power-source band ----------
          Flow {
            width: parent.width
            spacing: Style.space(12)

            LegendItem { label: "Average"; swatchColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, Model.CHART_STYLE.areaAlpha); swatchHeight: 10; outline: true }
            LegendItem { label: "Max"; swatchColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, Model.CHART_STYLE.maxAlpha); swatchHeight: Model.CHART_STYLE.maxWidth; dash: Model.CHART_STYLE.maxDash }
            LegendItem { label: "CPU"; swatchColor: root.accent; swatchHeight: Model.CHART_STYLE.cpuWidth }
            LegendItem { label: "GPU"; swatchColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, Model.CHART_STYLE.gpuAlpha); swatchHeight: Model.CHART_STYLE.gpuWidth; dash: Model.CHART_STYLE.gpuDash }
            LegendItem { label: "Threshold"; swatchColor: root.urgent; swatchHeight: 1; dash: Model.CHART_STYLE.thresholdDash }
          }

          Flow {
            width: parent.width
            spacing: Style.space(12)

            Text {
              textFormat: Text.PlainText
              text: "Source (band under the chart):"
              color: root.foreground
              opacity: 0.85
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            LegendItem { label: "Battery, measured"; swatchColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, Model.CHART_STYLE.batteryAlpha); swatchHeight: 5 }
            LegendItem { label: "AC, estimated"; swatchColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, Model.CHART_STYLE.acAlpha); swatchHeight: 5 }
            // Only where the machine measures on AC (RAPL psys); worded as the
            // status line and the "Total is" row word it.
            LegendItem { visible: Model.showsMeasuredAc(root.chart, root.current); label: Model.PSYS_BAND_LABEL; swatchColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, Model.CHART_STYLE.acMeasuredAlpha); swatchHeight: 5 }
            LegendItem { label: "Mixed"; swatchColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, Model.CHART_STYLE.mixedAlpha); swatchHeight: 5 }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.historyNote !== ""
            width: parent.width
            wrapMode: Text.Wrap
            text: root.historyNote
            color: root.foreground
            opacity: 0.7
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // ---------- stats ----------
          Row {
            width: parent.width
            spacing: Style.space(10)

            StatBlock { label: "MAX"; value: Model.formatWatts(root.rangeStats.max) }
            StatBlock { label: "AVERAGE"; value: Model.formatWatts(root.rangeStats.avg) }
            StatBlock { label: "CURRENT"; value: Model.currentStatText(root.rangeStats, root.current, root.stale) }
          }

          PanelSeparator { foreground: root.foreground }

          // ---------- current breakdown: dimmed and retitled when stale ----------
          PanelSectionHeader {
            text: Model.sampleTitle(root.current, root.stale)
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // Two columns, top to bottom: what draws the power, then where it
          // comes from (Model.breakdown orders the rows for this).
          Grid {
            id: breakdownGrid
            width: parent.width
            flow: Grid.TopToBottom
            rows: Model.BREAKDOWN_ROWS_PER_COLUMN
            columnSpacing: Style.space(20)
            rowSpacing: Style.spacing.labelGap
            opacity: root.stale ? 0.45 : 1
            readonly property real cellWidth: (width - columnSpacing) / 2

            Repeater {
              model: root.rows

              InfoPair {
                required property var modelData
                width: breakdownGrid.cellWidth
                label: modelData.label
                value: modelData.value
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // ---------- caps ----------
          PanelSectionHeader {
            text: "POWER CAPS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Grid {
            width: parent.width
            columns: 2
            columnSpacing: Style.space(20)
            rowSpacing: Style.spacing.labelGap
            opacity: root.stale ? 0.45 : 1

            InfoPair { width: breakdownGrid.cellWidth; label: "CPU limit"; value: root.caps.cpu }
            InfoPair {
              width: breakdownGrid.cellWidth
              label: "GPU limit"
              value: root.caps.gpu
              valueColor: root.caps.gpuCapped ? root.accent : root.foreground
            }
          }

          Text {
            textFormat: Text.PlainText
            readonly property string notes: root.stale ? "" : Model.sampleNotes(root.current)
            visible: notes !== ""
            width: parent.width
            wrapMode: Text.Wrap
            text: notes
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // ------------------------------------------------------------ components

  component InfoPair: Item {
    id: pair
    property string label: ""
    property string value: ""
    property color valueColor: root.foreground

    implicitHeight: Math.max(pairLabel.implicitHeight, pairValue.implicitHeight)

    Text {
      id: pairLabel
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: pair.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    // Elided at the end: the number that leads the value stays readable.
    Text {
      id: pairValue
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.left: pairLabel.right
      anchors.leftMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideRight
      text: pair.value
      color: pair.valueColor
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  component StatBlock: Column {
    id: stat
    property string label: ""
    property string value: ""

    width: (column.width - Style.space(20)) / 3
    spacing: Style.space(2)

    Text {
      textFormat: Text.PlainText
      text: stat.label
      color: root.foreground
      opacity: 0.7
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: Style.spaceReal(1.2)
    }

    Text {
      textFormat: Text.PlainText
      text: stat.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.heading
      font.bold: true
    }
  }

  // A legend swatch drawn the way the chart draws the series: `swatchHeight`
  // and `dash` ([on, off]) are in unscaled px, as in Model.CHART_STYLE, and go
  // through Style.space like the chart's.
  component LegendItem: Row {
    id: legend
    property string label: ""
    property color swatchColor: root.foreground
    property real swatchHeight: 1
    property var dash: null
    property bool outline: false
    readonly property real dashOn: legend.dash ? Math.max(1, Style.spaceReal(legend.dash[0])) : 0
    readonly property real dashOff: legend.dash ? Math.max(1, Style.spaceReal(legend.dash[1])) : 0

    spacing: Style.space(5)

    Item {
      id: swatch
      width: Style.space(24)
      height: legendLabel.implicitHeight
      anchors.verticalCenter: parent.verticalCenter

      Rectangle {
        visible: !legend.dash
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width
        height: Math.max(1, Style.spaceReal(legend.swatchHeight))
        color: legend.swatchColor

        Rectangle {
          visible: legend.outline
          width: parent.width
          height: 1
          color: root.foreground
          opacity: Model.CHART_STYLE.areaLineAlpha
        }
      }

      Row {
        visible: !!legend.dash
        anchors.verticalCenter: parent.verticalCenter
        spacing: legend.dashOff
        Repeater {
          model: legend.dash ? Math.max(1, Math.floor((swatch.width + legend.dashOff) / (legend.dashOn + legend.dashOff))) : 0
          Rectangle { width: legend.dashOn; height: Math.max(1, Style.spaceReal(legend.swatchHeight)); color: legend.swatchColor }
        }
      }
    }

    Text {
      id: legendLabel
      textFormat: Text.PlainText
      text: legend.label
      color: root.foreground
      opacity: 0.85
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
