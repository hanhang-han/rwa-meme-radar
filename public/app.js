const $ = (id) => document.getElementById(id);
const pct = (n) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`);
const fmtBnb = (n) => {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  if (v === 0) return "0 BNB";
  if (Math.abs(v) >= 1) return `${v.toFixed(2)} BNB`;
  if (Math.abs(v) >= 0.01) return `${v.toFixed(4)} BNB`;
  if (Math.abs(v) >= 0.0001) return `${v.toFixed(6)} BNB`;
  return `${v.toExponential(3)} BNB`;
};
const ago = (ts) => {
  if (!ts) return "—";
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return LANG === "zh" ? `${s}s 前` : `${s}s ago`;
  if (s < 3600) return LANG === "zh" ? `${Math.round(s / 60)}m 前` : `${Math.round(s / 60)}m ago`;
  return LANG === "zh" ? `${Math.round(s / 3600)}h 前` : `${Math.round(s / 3600)}h ago`;
};

let state = null;
let priceHistory = null;
let spreadChart, memeChart, funnelChart, histChart, barChart;
let tickerPaused = false;
const sortState = new Map();
const radarTab = { mode: "newest" };

const getWatch = () => new Set(JSON.parse(localStorage.getItem("watchlist") || "[]"));
const toggleWatch = (sym) => {
  const w = getWatch();
  w.has(sym) ? w.delete(sym) : w.add(sym);
  localStorage.setItem("watchlist", JSON.stringify([...w]));
  renderSpreads();
  renderAllTable();
};
const star = (sym) => `<button class="star ${getWatch().has(sym) ? "on" : ""}" data-star="${sym}" title="${t("watchHint")}">★</button>`;

function applyLang({ render = true } = {}) {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = t(el.dataset.i18n);
    if (el.dataset.i18n === "footer" || el.dataset.i18n === "spreadHint") el.textContent = v;
    else el.textContent = v;
  });
  $("refresh").title = t("refresh");
  $("search").placeholder = t("searchPh");
  $("langZh").classList.toggle("active", LANG === "zh");
  $("langEn").classList.toggle("active", LANG === "en");
  if (state && render) renderAll();
}

function heatLabel(v) {
  return v == null ? "—" : v < 34 ? t("heatCool") : v < 67 ? t("heatWarm") : t("heatHot");
}

function renderHealth() {
  const loops = ["bnb", "assets", "oracles", "prices", "scan", "radar", "fourmeme"];
  $("healthbar").innerHTML = loops
    .map((l) => {
      const last = state.health?.[l];
      const age = last ? (Date.now() - last) / 1000 : Infinity;
      const cls = age < 180 ? "ok" : age < 900 ? "warn" : "bad";
      return `<span class="hp ${cls}" title="${l}: ${ago(last)}"><i></i>${l}</span>`;
    })
    .join("");
}

function renderTicker() {
  if (!state.radar?.trades?.length) return;
  const items = state.radar.trades
    .map((tr) => {
      const cls = tr.kind === "buy" ? "up" : "down";
      const verb = tr.kind === "buy" ? t("buy") : t("sell");
      return `<span class="tk"><b class="${cls}">${verb}</b> ${fmtBnb(tr.bnb)} · ${escapeHtml(tr.symbol)} <i class="muted">${fmtTime(tr.tsSec * 1000)}</i></span>`;
    })
    .join('<span class="tk-sep">·</span>');
  const track = $("tickerTrack");
  track.innerHTML = items + '<span class="tk-sep">·</span>' + items;
}

function renderKpis() {
  $("bnbUsd").textContent = state.bnbUsd ? `$${fmtNum(state.bnbUsd, 2)}` : "--";
  $("updatedAt").textContent = state.now ? fmtTime(state.now) : "--";
  $("liveDot").className = "dot " + ((Date.now() - (state.now ?? 0)) < 90000 ? "" : "off");
  $("kAssets").textContent = state.assets.length;
  $("kPools").textContent = state.poolsFound;
  $("kMeme").textContent = state.fourMeme ? state.fourMeme.total : "--";
  $("kAttention").textContent = state.attention ?? "--";
  $("kAttentionNote").textContent = t("kAttentionNote");
}

function spreadRows() {
  return state.assets.filter((a) => a.spreadPct != null);
}

function renderSpreads() {
  let rows = spreadRows();
  const watch = getWatch();
  const s = sortState.get("spreadTable");
  if (s) {
    const val = (a) =>
      ({ symbol: a.symbol, underlying: a.underlying, oracle: a.oracle, dexPrice: a.dexPrice, lockedUsd: a.lockedUsd ?? 0, spreadPct: Math.abs(a.spreadPct), pool: a.pools[0]?.kind ?? "" }[s.key] ?? 0);
    rows.sort((a, b) => (typeof val(a) === "string" ? String(val(a)).localeCompare(String(val(b))) : val(b) - val(a)));
    if (s.dir === "asc") rows.reverse();
  } else {
    rows.sort((a, b) => Number(watch.has(b.symbol)) - Number(watch.has(a.symbol)) || Math.abs(b.spreadPct) - Math.abs(a.spreadPct));
  }
  const totalLocked = rows.reduce((s2, a) => s2 + (a.lockedUsd > 100 ? a.lockedUsd : 0), 0);
  $("spreadEmpty").style.display = rows.length ? "none" : "block";
  $("spreadEmpty").textContent = t("spreadEmpty");
  $("spreadTable").querySelector("tbody").innerHTML = rows
    .map((a) => {
      const cls = a.spreadPct >= 0 ? "pos" : "neg";
      const pool = a.pools[0];
      return `<tr>
      <td>${star(a.symbol)}</td>
      <td class="sym">${a.symbol}</td>
      <td class="muted">${a.underlying}</td>
      <td class="mono">$${fmtNum(a.oracle, 4)}</td>
      <td class="mono">$${fmtNum(a.dexPrice, 4)}</td>
      <td class="right mono">${a.lockedUsd > 100 ? "$" + fmtNum(a.lockedUsd, 0) : "—"}</td>
      <td><span class="tag on">${pool.kind}/${pool.quote}</span></td>
      <td class="right mono ${cls}">${pct(a.spreadPct)}</td>
    </tr>`;
    })
    .join("");
  const head = document.querySelector(".main-cols .panel .panel-head .hint");
  if (head && totalLocked > 0) head.textContent = `${t("thLocked")}: $${fmtNum(totalLocked, 0)}`;

  if (rows.length && window.echarts) {
    barChart = barChart || echarts.init($("spreadChart"), null, { renderer: "canvas" });
    barChart.setOption(
      {
        grid: { left: 8, right: 52, top: 8, bottom: 8, containLabel: true },
        xAxis: { type: "value", show: false },
        yAxis: {
          type: "category",
          data: rows.map((r) => r.symbol).slice(0, 8),
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: "#c9d4e3", fontFamily: "Menlo", fontSize: 11 },
        },
        series: [
          {
            type: "bar",
            data: rows.slice(0, 8).map((r) => ({ value: r.spreadPct, itemStyle: { color: r.spreadPct >= 0 ? "#2dd4a7" : "#ff5d5d" } })),
            barWidth: 14,
            label: { show: true, position: "right", formatter: (p) => pct(p.value), color: "#c9d4e3", fontFamily: "Menlo", fontSize: 11 },
          },
        ],
      },
      { replaceMerge: ["yAxis"] },
    );
  }
}

function renderHistory() {
  if (!priceHistory || !window.echarts) return;
  const spreadSeries = Object.keys(priceHistory.series).filter((k) => k.startsWith("spread:"));
  if (!spreadSeries.length) return;
  const key = spreadSeries[0];
  const pts = priceHistory.series[key];
  $("spreadChartMeta").textContent = key.split(":")[1] + ` · ${periodLabel(pts.at(-1)?.p)}`;
  const bands = [];
  let start = null, cur = null;
  for (const p of pts) {
    if (p.p !== cur) {
      if (start != null && cur === "closed") bands.push([{ xAxis: fmtTime(start) }, { xAxis: fmtTime(p.t) }]);
      cur = p.p;
      start = p.t;
    }
  }
  histChart = histChart || echarts.init($("historyChart"), null, { renderer: "canvas" });
  histChart.setOption({
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const p = ps[0];
        const pt = pts[p.dataIndex];
        return `${fmtTime(p.value[0], true)}<br/>${p.marker} <b>${pct(p.value[1])}</b><br/><span style="color:#5d6b82">${periodLabel(pt?.p)}</span>`;
      },
    },
    xAxis: { type: "time", axisLabel: { color: "#5d6b82", fontFamily: "Menlo", fontSize: 10 }, splitLine: { show: false } },
    yAxis: {
      type: "value",
      axisLabel: { color: "#5d6b82", fontFamily: "Menlo", fontSize: 10, formatter: (v) => v + "%" },
      splitLine: { lineStyle: { color: "#1d2736" } },
    },
    series: [
      {
        type: "line",
        data: pts.map((p) => [p.t, p.v]),
        showSymbol: false,
        lineStyle: { color: "#ffd43b", width: 1.5 },
        areaStyle: { color: "rgba(255,212,59,0.08)" },
        markArea: bands.length ? { silent: true, itemStyle: { color: "rgba(93,107,130,0.12)" }, data: bands } : undefined,
      },
    ],
  });
}

function renderRadar() {
  if (!state.radar) { $("radarEmpty").style.display = "block"; $("radarEmpty").textContent = t("radarEmpty"); return; }
  const r = state.radar;
  const list = [...r.tokens];
  if ((state.leadCandidates ?? []).length) {
    const inWindow = new Set(list.map((x) => (x.token || "").toLowerCase()));
    for (const c of state.leadCandidates) {
      if (inWindow.has(c.token.toLowerCase())) continue;
      list.push({ token: c.token, symbol: c.symbol, name: c.name, ts: c.ts ? Math.round(c.ts * 1000) : Math.round(c.firstSeen),
        buys: c.buys ?? 0, sells: c.sells ?? 0, volumeBnb: c.volumeBnb ?? null, volumeUsd: c.volumeUsd ?? null,
        match: c.match ?? null, _lead: c });
    }
  }
  const radarSort = sortState.get("radarTable");
  if (radarSort) {
    const radarValue = (x) => ({
      ts: x.ts ?? 0,
      symbol: x.symbol ?? "",
      name: x.name ?? "",
      trades: (x.buys ?? 0) + (x.sells ?? 0),
      volumeBnb: x.volumeBnb ?? -1,
    }[radarSort.key] ?? 0);
    list.sort((a, b) => typeof radarValue(a) === "string"
      ? String(radarValue(a)).localeCompare(String(radarValue(b)))
      : Number(radarValue(b)) - Number(radarValue(a)));
    if (radarSort.dir === "asc") list.reverse();
  } else if (radarTab.mode === "hot") {
    list.sort((a, b) => (b.volumeBnb ?? 0) - (a.volumeBnb ?? 0) || b.buys + b.sells - (a.sells + a.buys));
  }
  else if (radarTab.mode === "match") {
    list.sort((a, b) => (b.match ? 1 : 0) - (a.match ? 1 : 0) || b.buys + b.sells - (a.buys + a.sells));
  } else {
    list.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
  }
  // The candidate tab is a filter. Showing every unrelated token after the
  // matches made the tab look identical to “Newest” and obscured the useful
  // lead list.
  const rows = radarTab.mode === "match" ? list.filter((x) => x.match) : list;
  $("radarEmpty").style.display = rows.length ? "none" : "block";
  $("radarEmpty").textContent = t("radarEmpty");
  $("radarMeta").textContent = `${t("windowHint")} ${r.toBlock - r.fromBlock} ${t("blocksUnit")} · ${rows.length} ${t("newCoins")} · ${r.tradeCount} ${t("tradesUnit")}` + ((state.leadCandidates ?? []).length ? ` · 累积线索 ${state.leadCandidates.length}` : "");
  $("radarTable").querySelector("tbody").innerHTML = rows
    .slice(0, 60)
    .map((x) => {
      const time = x.ts ? fmtTime(x.ts * 1000) : "?";
      const m = x._lead
        ? `<span class="tag ${x._lead.status === "verified" ? "on" : "match"}">${x._lead.status === "verified" ? "✓ 已验证池 " + x._lead.pools : "线索 · " + x._lead.status}</span>`
        : x.match ? `<span class="tag match">${escapeHtml(x.match.ticker)} · 待验证</span>` : '<span class="muted">—</span>';
      const cls = x.match ? "sym hot" : "sym";
      const volume = x.volumeBnb != null
        ? `${fmtBnb(x.volumeBnb)}${x.volumeUsd != null ? ` · $${fmtNum(x.volumeUsd, 2)}` : ""}`
        : "—";
      return `<tr>
      <td class="mono muted">${time}</td>
      <td class="${cls}"><a href="#detail/56/${encodeURIComponent(x.token)}">${escapeHtml(x.symbol.slice(0, 14))}</a><br><code title="${escapeHtml(x.token)}">${escapeHtml(x.token)}</code></td>
      <td class="muted">${escapeHtml(x.name.slice(0, 30)) || "—"}</td>
      <td class="right mono">${x.buys}/${x.sells}</td>
      <td class="right mono">${volume}</td>
      <td>${m}</td>
    </tr>`;
    })
    .join("");
}

function renderMeme() {
  if (!state.fourMeme || !window.echarts) return;
  $("memeBlocks").textContent = `${state.fourMeme.fromBlock.toLocaleString()} → ${state.fourMeme.toBlock.toLocaleString()}`;
  const topics = state.fourMeme.byTopic.slice(0, 6);
  memeChart = memeChart || echarts.init($("memeChart"), null, { renderer: "canvas" });
  memeChart.setOption({
    grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", show: false },
    yAxis: {
      type: "category",
      data: topics.map((x) => `${x.topic.slice(0, 10)}…`).reverse(),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: "#5d6b82", fontFamily: "Menlo", fontSize: 10 },
    },
    series: [{ type: "bar", data: topics.map((x) => x.count).reverse(), barWidth: 12, itemStyle: { color: "#ffd43b" }, label: { show: true, position: "right", color: "#c9d4e3", fontFamily: "Menlo", fontSize: 10 } }],
  });
}

function renderFunnel() {
  if (!state.radar?.funnel || !window.echarts) return;
  const f = state.radar.funnel;
  $("funnelMeta").textContent = `${t("windowHint")} ${state.radar.toBlock - state.radar.fromBlock} ${t("blocksUnit")}`;
  funnelChart = funnelChart || echarts.init($("funnelChart"), null, { renderer: "canvas" });
  funnelChart.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c}" },
    series: [
      {
        type: "funnel",
        left: 8,
        right: 8,
        top: 12,
        bottom: 12,
        minSize: "18%",
        label: { color: "#0a0e14", fontFamily: "Menlo", fontSize: 11, formatter: (p) => `${p.name} ${p.value}` },
        itemStyle: { borderColor: "#10161f", borderWidth: 2 },
        data: [
          { value: f.created, name: t("funnelCreated"), itemStyle: { color: "#5d6b82" } },
          { value: f.hasTrade, name: t("funnelHasTrade"), itemStyle: { color: "#ffd43b" } },
          { value: f.active, name: t("funnelActive"), itemStyle: { color: "#2dd4a7" } },
        ],
      },
    ],
  });
}

function renderAllTable() {
  let rows = state.assets.filter((a) => {
    const q = $("search").value.trim().toLowerCase();
    return !q || a.symbol.toLowerCase().includes(q) || a.underlying.toLowerCase().includes(q);
  });
  const watch = getWatch();
  const s = sortState.get("allTable");
  const val = (a) => ({ symbol: a.symbol, underlying: a.underlying, chg24h: a.chg24h ?? -999, oracle: a.oracle ?? -1, period: a.period, pools: a.pools.length }[s?.key ?? "pools"] ?? 0);
  rows.sort((a, b) => (typeof val(a) === "string" ? String(val(a)).localeCompare(String(val(b))) : val(b) - val(a)));
  if (s?.dir === "asc") rows.reverse();
  if (!s) rows.sort((a, b) => Number(watch.has(b.symbol)) - Number(watch.has(a.symbol)));
  $("allTable").querySelector("tbody").innerHTML = rows
    .slice(0, 300)
    .map((a) => {
      const pool = a.pools.length
        ? `<span class="tag on">${a.pools[0].kind}</span>`
        : `<span class="tag">${t("rfq")}</span>`;
      const chg = a.chg24h == null ? "—" : `<span class="${a.chg24h >= 0 ? "pos" : "neg"}">${pct(a.chg24h)}</span>`;
      const detail = a.pools.length
        ? `<tr class="detail"><td colspan="7">
            <div class="drawer">
              <span>${t("poolAddr")}: <a href="https://www.oklink.com/bsc/address/${a.pools[0].address}" target="_blank" rel="noopener">${a.pools[0].address.slice(0, 10)}…${a.pools[0].address.slice(-6)} ↗</a></span>
              <span>${t("oracleUpdatedAt")}: ${ago(a.oracleAt)}</span>
            </div></td></tr>`
        : "";
      return `<tr class="master">
      <td>${star(a.symbol)}</td>
      <td class="sym">${a.symbol}</td>
      <td class="muted">${a.underlying}</td>
      <td class="right mono">${chg}</td>
      <td class="mono">${a.oracle ? `$${fmtNum(a.oracle, 4)}` : "—"}</td>
      <td class="muted">${periodLabel(a.period)}${a.halted ? ` · <b class="neg">${t("halted")}</b>` : ""}</td>
      <td>${pool}</td>
    </tr>${detail}`;
    })
    .join("");
}

function renderAll() {
  if(window.RadarV2){$("updatedAt").textContent=new Date().toLocaleTimeString();document.dispatchEvent(new Event("stateupdated"));return;}
  renderKpis();
  // The BSC reference dashboard contains several full tables and charts. It is
  // only needed when the user explicitly opens that historical view; building
  // it on every unified-data refresh blocks unrelated interactions.
  const legacyActive = document.getElementById("network")?.value === "56";
  if (!legacyActive) {
    document.dispatchEvent(new Event("stateupdated"));
    return;
  }
  renderHealth();
  renderTicker();
  renderSpreads();
  renderRadar();
  renderMeme();
  renderFunnel();
  renderAllTable();
  renderHistory();
  document.dispatchEvent(new Event("stateupdated"));
}

async function tick() {
  let sRes, hRes;
  try {
    [sRes, hRes] = await Promise.all([fetch("/api/dashboard"), Promise.resolve({ok:false})]);
  } catch (e) {
    $("updatedAt").textContent = LANG === "zh" ? "连接失败" : "connection lost";
    return;
  }
  if (!sRes.ok) {
    $("updatedAt").textContent = LANG === "zh" ? "连接失败" : "connection lost";
    return;
  }
  try {
    state = await sRes.json();
    priceHistory = hRes.ok ? await hRes.json() : null;
    renderAll();
  } catch (e) {
    console.error("render failed", e);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

document.addEventListener("click", (e) => {
  const st = e.target.closest("[data-star]");
  if (st) {
    e.stopPropagation();
    toggleWatch(st.dataset.star);
    return;
  }
  const th = e.target.closest("th[data-sort]");
  if (th) {
    const table = th.closest("table").id;
    const key = th.dataset.sort;
    const cur = sortState.get(table);
    sortState.set(table, cur?.key === key ? { key, dir: cur.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" });
    th.closest("tr").querySelectorAll("th").forEach((h) => (h.className = h.className.replace(/ (sort-asc|sort-desc)/, "")));
    th.classList.add(sortState.get(table).dir === "desc" ? "sort-desc" : "sort-asc");
    if (table === "spreadTable") renderSpreads();
    else if (table === "allTable") renderAllTable();
    return;
  }
  const master = e.target.closest("tr.master");
  if (master) {
    const next = master.nextElementSibling;
    if (next?.classList.contains("detail")) next.classList.toggle("open");
  }
});

$("radarTabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (!b) return;
  radarTab.mode = b.dataset.tab;
  document.querySelectorAll("#radarTabs .tab").forEach((x) => x.classList.toggle("active", x === b));
  renderRadar();
});

$("langZh").addEventListener("click", () => setLang("zh"));
$("langEn").addEventListener("click", () => setLang("en"));
$("refresh").addEventListener("click", tick);
$("search").addEventListener("input", () => renderAllTable());
$("tickerPause").addEventListener("click", () => {
  tickerPaused = !tickerPaused;
  $("tickerTrack").style.animationPlayState = tickerPaused ? "paused" : "running";
  $("tickerPause").textContent = tickerPaused ? "▶" : "❚❚";
  $("tickerPause").classList.toggle("paused", tickerPaused);
});
window.addEventListener("resize", () => [barChart, histChart, memeChart, funnelChart].forEach((c) => c?.resize()));

initLang();
applyLang();
tick();
setInterval(tick, 30000);
