import QtQuick
import qs.Commons
import "Model.js" as Model

// MRTG-style history chart. Every coordinate comes from Model.chartLayout; this
// file only strokes and fills what it is handed.
//
// Repaints are driven by `chart` changing, and `chart` is a binding over the
// parsed history, the range and two scalar properties — so new data reaches it
// at most once per Model.HISTORY_THROTTLE_MS, however often the files change
// (see the history Loader in BarWidget.qml). current.json updating every two
// seconds does not repaint it.
//
// Text is painted with fillText from strings the model built out of numbers;
// nothing file-sourced is drawn here. Widths, dashes and margins go through
// Style.space/spaceReal, so the chart scales with the shell's spacing.
// How each series looks is Model.CHART_STYLE, shared with the legend.
Canvas {
  id: root

  property var chart: null
  property color foreground: "white"
  property color accent: "white"
  property color urgent: "red"
  // Behind the threshold label, so it stays legible over the data.
  property color labelBackground: "black"
  property string fontFamily: "monospace"
  property int fontSize: 10
  property string emptyText: "No history for this range yet"

  // Plot margins, in pixels: room for the watt labels (the top one carries
  // " W") on the left, and the power-source band plus time labels under it.
  readonly property var metrics: ({
    left: Math.ceil(fontSize * 4.2), right: Style.space(6), top: Math.ceil(fontSize * 0.8),
    band: Math.max(Style.space(4), Math.round(fontSize * 0.6)), bottom: Math.ceil(fontSize * 1.6)
  })
  readonly property var look: Model.CHART_STYLE

  function px(value) { return Style.spaceReal(value) }

  function rgba(c, a) {
    return "rgba(" + Math.round(c.r * 255) + "," + Math.round(c.g * 255) + "," + Math.round(c.b * 255) + "," + a + ")"
  }

  function strokePolys(ctx, polys, color, width) {
    ctx.strokeStyle = color
    ctx.lineWidth = width
    for (var i = 0; i < polys.length; i++) {
      var p = polys[i]
      ctx.beginPath()
      ctx.moveTo(p[0][0], p[0][1])
      for (var j = 1; j < p.length; j++) ctx.lineTo(p[j][0], p[j][1])
      ctx.stroke()
    }
  }

  function strokeDashed(ctx, polys, color, width, on, off) {
    ctx.strokeStyle = color
    ctx.lineWidth = width
    ctx.beginPath()
    // One budget for the whole series, however many runs the gaps cut it into.
    var budget = Model.MAX_DASH_SEGMENTS
    for (var i = 0; i < polys.length && budget > 0; i++) {
      var segs = Model.dashSegments(polys[i], on, off, budget)
      budget -= segs.length
      for (var j = 0; j < segs.length; j++) {
        ctx.moveTo(segs[j][0], segs[j][1])
        ctx.lineTo(segs[j][2], segs[j][3])
      }
    }
    ctx.stroke()
  }

  function spanColor(mode) {
    if (mode === "battery") return rgba(root.accent, root.look.batteryAlpha)
    if (mode === "mixed") return rgba(root.accent, root.look.mixedAlpha)
    if (mode === "acMeasured") return rgba(root.foreground, root.look.acMeasuredAlpha)
    if (mode === "ac") return rgba(root.foreground, root.look.acAlpha)
    return rgba(root.foreground, root.look.unknownAlpha)
  }

  // Text on a backdrop, so it stays legible over grid lines and data.
  function labelBox(ctx, text, x, y, align) {
    var w = ctx.measureText(text).width
    var pad = root.px(3)
    var left = align === "right" ? x - w : align === "center" ? x - w / 2 : x
    ctx.fillStyle = rgba(root.labelBackground, 0.85)
    ctx.fillRect(left - pad, y - root.fontSize * 0.6, w + pad * 2, root.fontSize * 1.2)
  }

  onChartChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()
  onForegroundChanged: requestPaint()
  onAccentChanged: requestPaint()
  onUrgentChanged: requestPaint()
  onLabelBackgroundChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    if (!ctx) return
    ctx.reset()
    var lay = Model.chartLayout(root.chart, width, height, root.metrics)
    var plot = lay.plot
    ctx.font = Model.canvasFont(root.fontSize, root.fontFamily)
    ctx.textBaseline = "middle"

    // battery periods shaded behind everything, so they read inside the plot too
    for (var b = 0; b < lay.spans.length; b++) {
      if (lay.spans[b].mode !== "battery") continue
      ctx.fillStyle = rgba(root.accent, 0.08)
      ctx.fillRect(lay.spans[b].x, plot.y, lay.spans[b].w, plot.h)
    }

    // grid + watt axis
    ctx.lineWidth = 1
    ctx.strokeStyle = rgba(root.foreground, 0.12)
    ctx.fillStyle = rgba(root.foreground, 0.65)
    ctx.textAlign = "right"
    ctx.beginPath()
    for (var y = 0; y < lay.yTicks.length; y++) {
      var gy = Math.round(lay.yTicks[y].y) + 0.5
      ctx.moveTo(plot.x, gy)
      ctx.lineTo(plot.x + plot.w, gy)
    }
    for (var x = 0; x < lay.xTicks.length; x++) {
      var gx = Math.round(lay.xTicks[x].x) + 0.5
      ctx.moveTo(gx, plot.y)
      ctx.lineTo(gx, plot.y + plot.h)
    }
    ctx.stroke()
    for (var yl = 0; yl < lay.yTicks.length; yl++)
      ctx.fillText(lay.yTicks[yl].label, plot.x - root.px(4), lay.yTicks[yl].y)

    // time axis labels under the band, kept whole inside the plot's width
    ctx.textAlign = "center"
    var labelY = lay.bandY + lay.bandH + root.fontSize * 0.8
    for (var xl = 0; xl < lay.xTicks.length; xl++) {
      var label = lay.xTicks[xl].label
      var lx = Model.clampLabelX(lay.xTicks[xl].x, ctx.measureText(label).width, plot.x, plot.w)
      ctx.fillText(label, lx, labelY)
    }

    // total average: filled area down to the axis, one shape per gap-free run
    ctx.fillStyle = rgba(root.foreground, root.look.areaAlpha)
    for (var a = 0; a < lay.area.length; a++) {
      var poly = lay.area[a]
      ctx.beginPath()
      ctx.moveTo(poly[0][0], lay.baseY)
      for (var k = 0; k < poly.length; k++) ctx.lineTo(poly[k][0], poly[k][1])
      ctx.lineTo(poly[poly.length - 1][0], lay.baseY)
      ctx.closePath()
      ctx.fill()
    }
    strokePolys(ctx, lay.area, rgba(root.foreground, root.look.areaLineAlpha), 1)

    // Max thin dotted, CPU thick solid accent, GPU thick long-dashed accent:
    // told apart by pattern and weight even where accent == foreground.
    strokeDashed(ctx, lay.max, rgba(root.foreground, root.look.maxAlpha), Math.max(1, root.px(root.look.maxWidth)),
      Math.max(1, root.px(root.look.maxDash[0])), Math.max(1, root.px(root.look.maxDash[1])))
    strokePolys(ctx, lay.cpu, rgba(root.accent, 1), root.px(root.look.cpuWidth))
    strokeDashed(ctx, lay.gpu, rgba(root.accent, root.look.gpuAlpha), root.px(root.look.gpuWidth),
      root.px(root.look.gpuDash[0]), root.px(root.look.gpuDash[1]))

    // threshold
    var ty = Math.round(lay.thresholdY) + 0.5
    strokeDashed(ctx, [[[plot.x, ty], [plot.x + plot.w, ty]]], rgba(root.urgent, 1), 1,
      root.px(root.look.thresholdDash[0]), root.px(root.look.thresholdDash[1]))
    if (root.chart) {
      var limitText = Model.formatWatts(root.chart.threshold)
      var limitX = plot.x + plot.w - root.px(3)
      var limitY = ty - root.fontSize * 0.75
      labelBox(ctx, limitText, limitX, limitY, "right")
      ctx.fillStyle = rgba(root.urgent, 1)
      ctx.textAlign = "right"
      ctx.fillText(limitText, limitX, limitY)
    }

    // axis line + power-source band
    ctx.strokeStyle = rgba(root.foreground, 0.5)
    ctx.beginPath()
    ctx.moveTo(plot.x, Math.round(lay.baseY) + 0.5)
    ctx.lineTo(plot.x + plot.w, Math.round(lay.baseY) + 0.5)
    ctx.stroke()
    for (var s = 0; s < lay.spans.length; s++) {
      ctx.fillStyle = spanColor(lay.spans[s].mode)
      ctx.fillRect(lay.spans[s].x, lay.bandY, lay.spans[s].w, lay.bandH)
    }

    if (lay.empty) {
      var cx = plot.x + plot.w / 2
      var cy = plot.y + plot.h / 2
      labelBox(ctx, root.emptyText, cx, cy, "center")
      ctx.fillStyle = rgba(root.foreground, 0.7)
      ctx.textAlign = "center"
      ctx.fillText(root.emptyText, cx, cy)
    }
  }
}
