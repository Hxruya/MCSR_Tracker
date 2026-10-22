/* Charts for SiftStats (Chart.js 4, loaded from cdnjs on pages that need it).
 *
 * Each chart is a <canvas data-chart="ID"> next to <script type="application/json" id="ID">:
 *   { kind: "line-time" | "column" | "hbar" | "grouped",
 *     labels: [...],                         // categories (not for line-time)
 *     series: [{ name, slot: 1|2, data }],   // line-time data: [{x: epochMs, y}]
 *     format: "int" | "pct" | "elo" | "sec" | "pp", // value formatting for axis + tooltip
 *     stacked, diverging: bool,              // histogram split in two classes / bars around zero
 *     higherIsBetter: bool,                  // diverging: positive bars take slot 1 (default: negative)
 *     legend: [{ name, slot }],              // legend keys when they differ from the series
 *     extra: { label: [..] } }               // optional extra tooltip line per category
 * Colours come from CSS tokens (--series-1/2, --grid, --axis, ...), checked for colour-blind contrast,
 * and charts are rebuilt when the colour scheme changes.
 */
(function () {
  "use strict";
  if (!window.Chart) return;

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const fmt = {
    int: (v) => (Number(v) || 0).toLocaleString(),   // || 0 turns -0 into 0
    elo: (v) => Math.round(v).toLocaleString(),
    pct: (v) => `${Number(v).toFixed(v % 1 ? 1 : 0)}%`,
    sec: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 && v % 1 ? 1 : 0)}s`,
    pp: (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(Math.abs(v) < 10 && v % 1 ? 1 : 0)} pp`,
    date: (v) => new Date(v).toISOString().slice(0, 10),
    month: (v) => { const d = new Date(v); return `${d.toLocaleDateString("en", { month: "short", timeZone: "UTC" })} '${String(d.getUTCFullYear()).slice(2)}`; },
  };

  function theme() {
    return {
      s1: css("--series-1"), s2: css("--series-2"), grid: css("--grid"), axis: css("--axis"),
      ink: css("--ink"), ink2: css("--ink-2"), muted: css("--muted"), surface: css("--surface"),
      border: css("--border-strong"),
    };
  }

  function wash(hex, alpha) {
    const n = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function baseOptions(t, spec) {
    const f = fmt[spec.format] || fmt.int;
    const tickFont = { family: "Onest, system-ui, sans-serif", size: 11 };
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      layout: { padding: { top: 6, right: 8 } },
      plugins: {
        legend: { display: false },  // HTML legend is rendered by the template for >= 2 series
        tooltip: {
          backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2, borderColor: t.border, borderWidth: 1,
          padding: 10, cornerRadius: 8, boxPadding: 4, usePointStyle: true,
          titleFont: { family: "Onest", weight: "600", size: 12 }, bodyFont: { family: "Onest", size: 12 },
          callbacks: {
            label: (ctx) => {
              const v = spec.kind === "hbar" ? ctx.parsed.x : ctx.parsed.y;
              return ` ${ctx.dataset.label}: ${f(v)}`;
            },
            afterLabel: (ctx) => {
              const extra = spec.extra && spec.extra[ctx.dataset.label];
              return extra ? `   ${extra[ctx.dataIndex]}` : "";
            },
          },
        },
      },
      scales: {
        x: { grid: { color: t.grid, drawTicks: false }, border: { color: t.axis }, ticks: { color: t.muted, font: tickFont, padding: 6 } },
        y: { grid: { color: t.grid, drawTicks: false }, border: { display: false }, ticks: { color: t.muted, font: tickFont, padding: 8 } },
      },
    };
  }

  function build(canvas) {
    const spec = JSON.parse(document.getElementById(canvas.dataset.chart).textContent);
    const t = theme();
    const color = (s) => (s.slot === 2 ? t.s2 : t.s1);
    const opts = baseOptions(t, spec);
    const valueTick = fmt[spec.format] || fmt.int;

    if (spec.kind === "line-time") {
      // first series is the measure (filled); later ones are overlays such as a moving average (no fill)
      const s = spec.series[0];
      opts.parsing = false;
      opts.interaction = { mode: "nearest", axis: "x", intersect: false };  // crosshair-style hover
      opts.plugins.decimation = { enabled: true, algorithm: "lttb", samples: 600 };
      opts.scales.x.type = "linear";
      opts.scales.x.min = s.data[0].x;                    // axis spans exactly the data
      opts.scales.x.max = s.data[s.data.length - 1].x;
      opts.scales.x.grid.display = false;
      opts.scales.x.ticks.callback = (v) => fmt.month(v);
      opts.scales.x.ticks.maxTicksLimit = 8;
      opts.scales.y.ticks.callback = (v) => valueTick(v);
      opts.plugins.tooltip.callbacks.title = (items) => fmt.date(items[0].parsed.x);
      return new Chart(canvas, {
        type: "line",
        data: { datasets: spec.series.map((d, i) => ({
          label: d.name, data: d.data, borderColor: color(d),
          backgroundColor: i === 0 ? wash(color(d), 0.10) : color(d), fill: i === 0 ? "start" : false,
          borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: t.surface,
          pointHoverBackgroundColor: color(d), tension: 0, borderJoinStyle: "round", borderCapStyle: "round",
        })) },
        options: opts,
      });
    }

    const horizontal = spec.kind === "hbar";
    const datasets = spec.series.map((s) => ({
      label: s.name, data: s.data, backgroundColor: color(s), hoverBackgroundColor: color(s),
      maxBarThickness: 24, borderRadius: 4, borderSkipped: "start",       // rounded data-end, square baseline
      borderColor: t.surface, borderWidth: spec.series.length > 1 ? { top: 0, bottom: 0, left: 1, right: 1 } : 0,
      categoryPercentage: 0.7, barPercentage: spec.series.length > 1 ? 0.92 : 0.8,
    }));
    opts.indexAxis = horizontal ? "y" : "x";
    opts.interaction = { mode: "index", intersect: false };
    const valueAxis = horizontal ? "x" : "y";
    const catAxis = horizontal ? "y" : "x";
    opts.scales[catAxis].grid.display = false;
    opts.scales[valueAxis].beginAtZero = true;
    opts.scales[valueAxis].ticks.callback = (v) => valueTick(v);
    if (spec.format === "pct") opts.scales[valueAxis].max = 100;
    if (spec.stacked) {  // e.g. a histogram whose bins are split into two classes (each bin in one class)
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      datasets.forEach((d) => { d.barPercentage = 0.92; d.categoryPercentage = 0.92; d.borderWidth = 0; });
      opts.plugins.tooltip.filter = (item) => item.parsed.y > 0;
      opts.scales.x.ticks.autoSkip = true;
      opts.scales.x.ticks.maxTicksLimit = 12;
    }
    if (horizontal) {
      opts.scales.y.ticks.color = t.ink2; opts.scales.y.border = { color: t.axis }; opts.scales.x.border = { display: false };
      opts.scales.y.ticks.autoSkip = false;   // every category labelled (the container is sized for it)
      opts.layout.padding.left = 14;  // room for the first glyph of long category labels
    }
    if (spec.diverging) {
      // one measure around zero: the better side takes slot 1 (negative by default, e.g. seconds faster;
      // higherIsBetter for e.g. win-rate points), the other side slot 2
      const vals = spec.series[0].data;
      const better = (v) => (spec.higherIsBetter ? v > 0 : v < 0);
      datasets[0].backgroundColor = vals.map((v) => (better(v) ? t.s1 : t.s2));
      datasets[0].hoverBackgroundColor = datasets[0].backgroundColor;
      datasets[0].borderSkipped = false;
      delete opts.scales[valueAxis].beginAtZero;
      const m = Math.max(5, ...vals.map((v) => Math.abs(v)));
      opts.scales[valueAxis].min = -Math.ceil(m / 5) * 5;
      opts.scales[valueAxis].max = Math.ceil(m / 5) * 5;
      opts.scales[valueAxis].grid.color = (ctx) => (ctx.tick && ctx.tick.value === 0 ? t.axis : t.grid);
    }
    return new Chart(canvas, { type: "bar", data: { labels: spec.labels, datasets }, options: opts });
  }

  let charts = [];
  function renderAll() {
    charts.forEach((c) => c.destroy());
    charts = Array.from(document.querySelectorAll("canvas[data-chart]")).map(build).filter(Boolean);
  }
  renderAll();
  document.addEventListener("themechange", renderAll);  // fired by theme.js (toggle or OS change)
})();
