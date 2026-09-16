// Four views share the existing polling and chart rendering pipeline.
const workspace = document.createElement('main');
workspace.className = 'radar-workspace';
if(!window.RadarV2)for(const el of document.querySelectorAll('#healthbar,.ticker-wrap,.kpis,.cols'))el.hidden=false;
workspace.innerHTML = `
<nav class="workspace-nav" aria-label="主导航">
  <a href="#live">Live Radar</a><a href="#meme">Meme Radar</a><a href="#stock">Stock Radar</a><a href="#detail">Meme Detail</a>
  <select id="network" hidden aria-hidden="true" tabindex="-1"><option value="196">统一资产目录</option><option value="56">历史记录</option></select>
</nav>
<div id="sourceStatus" role="status" class="source-status">正在获取数据…</div>
<section id="view-live" class="view"><div class="hero"><span class="eyebrow">DISCOVER · VERIFY · UNDERSTAND</span><h2>Meme 与股票，关系有据可查。</h2><p>从名称线索出发，区分资金证据与统计联动。</p></div><div id="signalMetrics" class="kpis"></div><div class="cols main-cols"><section class="panel"><h2>最新关联线索</h2><div id="liveCandidates"></div></section><section class="panel"><h2>行业篮子 · 方法打样</h2><p class="hint">芯片 / 交易所 / 支付 / 托管 / 加密国库</p><div id="sectorValues" class="empty">等待已验证直接配对成分的价格与市值样本</div><p>基期 100，以总供应市值估算权重。只有已验证直接配对、且有共同价格与市值样本的成分才会进入篮子；名称线索不会直接改变指数。</p></section></div><section class="panel"><h2>最新链上新币</h2><div id="liveTokens"></div><a href="#meme">查看全部新币 →</a></section><section class="panel"><h2>关系分类 · v1 草案</h2><div id="taxonomy" class="taxonomy"></div></section></section>
<section id="view-meme" class="view" hidden><div class="page-heading"><h2>Meme Radar</h2><p>发现线索，查看 CA，再核实关系证据。</p></div><div id="memeUnavailable" class="panel empty"></div><div id="memeLegacy"></div></section>
<section id="view-stock" class="view" hidden><div class="page-heading"><h2>Stock Links</h2><p>股票代币行情与对应的 Meme 线索分开呈现。</p></div><div id="okxStocks" class="panel"></div><div id="stockLegacy"></div></section>
<section id="view-detail" class="view" hidden><div id="memeDetail"></div></section>`;
document.querySelector('header').after(workspace);
if(window.RadarV2){workspace.insertAdjacentHTML('beforeend','<section id="view-pair" class="view" hidden></section><section id="view-events" class="view" hidden></section>');document.querySelector('.workspace-nav a[href="#detail"]').href='#pair';}
const stockLegacy = document.getElementById('stockLegacy');
const memeLegacy = document.getElementById('memeLegacy');
const radarPanel = document.getElementById('radarTabs').closest('section');
memeLegacy.append(radarPanel);
// Keep each legacy panel on the page that owns its data. The original page
// was one long dashboard, which made Stock Radar show meme charts and made
// the four top-level tabs look interchangeable.
const legacyHealth = document.querySelector('body > .healthbar');
const legacyTicker = document.querySelector('body > .ticker-wrap');
const legacyKpis = document.querySelector('body > section.kpis');
const legacyMainCols = document.querySelector('body > .cols.main-cols');
const legacyCols = document.querySelector('body > .cols:not(.main-cols)');
const legacyDisclosure = document.querySelector('body > .disclosures');
const liveView = document.getElementById('view-live');
if (legacyHealth) liveView.prepend(legacyHealth);
if (legacyTicker) liveView.append(legacyTicker);
if (legacyKpis) liveView.append(legacyKpis);
if (legacyMainCols) stockLegacy.append(legacyMainCols);
if (legacyCols) {
  const [memeChartPanel, funnelPanel, stockTablePanel] = [...legacyCols.children];
  if (memeChartPanel) memeLegacy.append(memeChartPanel);
  if (funnelPanel) memeLegacy.append(funnelPanel);
  if (stockTablePanel) stockLegacy.append(stockTablePanel);
  legacyCols.remove();
}
if (legacyDisclosure) liveView.append(legacyDisclosure);
document.querySelector('.brand .sub').removeAttribute('data-i18n');
document.querySelector('.brand .sub').textContent = 'Onchain Relationship Intelligence';
function workspaceText(zh, en) { return typeof LANG !== 'undefined' && LANG === 'en' ? en : zh; }
function applyWorkspaceLanguage() {
  const nav = document.querySelectorAll('.workspace-nav > a');
  [window.RadarV2?'首页|Home':'实时雷达|Live Radar', 'MEME|MEME', '股票|Stocks', window.RadarV2?'交易池分析|Pool Analysis':'资产详情|Asset Detail'].forEach((pair, index) => {
    const [zh, en] = pair.split('|'); if (nav[index]) nav[index].textContent = workspaceText(zh, en);
  });
  const copy = [
    ['.workspace-nav','aria-label','主导航','Main navigation'],
    ['#view-meme > .page-heading h2',null,'Meme 雷达','Meme Radar'],
    ['#view-meme > .page-heading p',null,'发现线索，查看 CA，再核实关系证据。','Discover leads, inspect the CA, then verify relationship evidence.'],
    ['#view-stock > .page-heading h2',null,'股票关联','Stock Links'],
    ['#view-stock > .page-heading p',null,'股票代币行情与对应的 Meme 线索分开呈现。','Stock-token market data and Meme leads are presented separately.'],
    ['#view-live > .hero:not(.x-hero) h2',null,'Meme 与股票，关系有据可查。','Meme and stocks: relationships backed by evidence.'],
    ['#view-live > .hero:not(.x-hero) p',null,'从名称线索出发，区分资金证据与统计联动。','Start with name leads, then separate capital evidence from statistical correlation.'],
    ['#view-live > .cols.main-cols .panel:first-child h2',null,'最新关联线索','Latest relationship leads'],
    ['#view-live > .cols.main-cols .panel:nth-child(2) h2',null,'行业篮子 · 方法打样','Sector baskets · methodology preview'],
    ['#view-live > .cols.main-cols .panel:nth-child(2) .hint',null,'芯片 / 交易所 / 支付 / 托管 / 加密国库','Semiconductors / Exchanges / Payments / Custody / Crypto treasury'],
    ['#view-live > .panel:nth-of-type(1) h2',null,'最新链上新币','Latest on-chain tokens'],
    ['#view-live > .panel:nth-of-type(1) a',null,'查看全部新币 →','View all tokens →'],
    ['#view-live > .panel:nth-of-type(2) h2',null,'关系分类 · v1 草案','Relationship taxonomy · v1 draft']
  ];
  copy.forEach(([selector, attribute, zh, en]) => document.querySelectorAll(selector).forEach(el => {
    if (attribute) el.setAttribute(attribute, workspaceText(zh, en)); else el.textContent = workspaceText(zh, en);
  }));
  document.querySelector('.brand .sub').textContent = workspaceText('链上关系情报', 'Onchain Relationship Intelligence');
}
applyWorkspaceLanguage();
const network = document.getElementById('network');
let networkInitialized = false;
try {
  const saved = localStorage.getItem('radar-network');
  if (saved === '56' || saved === '196') { network.value = saved; networkInitialized = true; }
} catch { /* private browsing can disable storage */ }

const e = escapeHtml;
const shortAddress = a => a ? a.slice(0, 8) + '…' + a.slice(-6) : '—';
function detailLink(token) { return '#detail/56/' + encodeURIComponent(token); }
function linkForToken(address, chain = '56') {
  // OKX explorer addresses preserve chain attribution; no speculative swap links.
  return `https://www.oklink.com/${chain === '196' ? 'xlayer' : 'bsc'}/address/${encodeURIComponent(address)}`;
}
function candidateRow(x) {
  if (x._lead) {
    const st = x._lead.status === "verified" ? workspaceText(`已验证池 ${x._lead.pools}`, `Verified pools ${x._lead.pools}`) : x._lead.status;
    return `<a class="candidate" href="${detailLink(x.token)}"><strong>${e(x.symbol)}</strong><span>${e(x.name)}</span><span>${x.buys ?? 0} ${workspaceText('买','buy')} / ${x.sells ?? 0} ${workspaceText('卖','sell')} · ${x.volumeBnb != null ? fmtBnb(x.volumeBnb) : workspaceText('成交额待采集','Volume pending')}</span><span class="tag match">${workspaceText('累积线索','Accumulated lead')} · ${e(st)}</span><span>${workspaceText('查看证据','View evidence')} →</span></a>`;
  }
  return `<a class="candidate" href="${detailLink(x.token)}"><strong>${e(x.symbol)}</strong><span>${e(x.match?.ticker || workspaceText('未识别','Unidentified'))}</span><span class="tag match">${workspaceText('名称线索 · 待验证','Name lead · pending')}</span><span>${workspaceText('查看证据','View evidence')} →</span></a>`;
}
function renderWorkspace() {
  if (!state) return;
  if(window.RadarV2){window.RadarV2.render(state);return;}
  const okx = state.okx || { status: 'unconfigured', tokens: [] };
  if (!networkInitialized) {
    network.value = '196';
    networkInitialized = true;
  }
  const bsc = network.value === '56';
  for (const el of document.querySelectorAll('#view-live > :not(#xlayerLive)')) el.hidden = !bsc;
  if (document.getElementById('xlayerLive')) document.getElementById('xlayerLive').hidden = bsc;
  if (document.getElementById('xlayerMeme')) document.getElementById('xlayerMeme').hidden = bsc;
  if (legacyHealth) legacyHealth.hidden = !bsc;
  if (legacyTicker) legacyTicker.hidden = !bsc;
  if (legacyKpis) legacyKpis.hidden = !bsc;
  document.getElementById('bnbUsd').closest('.metric').hidden = !bsc;
  document.querySelector('footer').removeAttribute('data-i18n');
  document.querySelector('footer').textContent = workspaceText('资产、关系与证据按统一结构归档；可用性以各记录更新时间为准。', 'Assets, relationships, and evidence use one common structure; availability follows each record’s update time.');
  if (!bsc && window.XLayerUI) {
    memeLegacy.hidden = true; stockLegacy.hidden = true;
    document.getElementById('memeUnavailable').hidden = true;
    document.getElementById('okxStocks').hidden = false;
    window.XLayerUI.render(state);
    renderDetail();
    return;
  }
  const tokens = bsc ? state.radar?.tokens || [] : [];
  const inWindow = new Set(tokens.map(x => (x.token || "").toLowerCase()));
  const extraLeads = (state.leadCandidates ?? []).filter(c => !inWindow.has((c.token || "").toLowerCase())).map(c => ({ token: c.token, symbol: c.symbol, name: c.name, buys: c.buys ?? 0, sells: c.sells ?? 0, volumeBnb: c.volumeBnb ?? null, volumeUsd: c.volumeUsd ?? null, match: c.match ?? null, verification: c.verification ?? null, analytics: c.analytics ?? null, relation: c.relation ?? null, creator: c.creator ?? '', block: c.block ?? 0, ts: c.ts ?? null, supply: c.supply ?? null, lastPriceBnb: c.lastPriceBnb ?? null, lastPriceUsd: c.lastPriceUsd ?? null, _lead: c }));
  const candidates = tokens.filter(x => x.match).concat(extraLeads);
  const sourceText = { unconfigured: 'X Layer：OKX 数据源尚未配置，暂无可展示的已接入资产。', ready: 'X Layer：OKX RWA 数据已接入。', stale: 'X Layer：OKX 暂时无法更新，下方为上次成功获取的数据。', error: 'X Layer：OKX 数据暂不可用，请稍后重试。' };
  document.getElementById('sourceStatus').textContent = bsc
    ? `${okx.status === "unconfigured" ? "X Layer 尚未接入，当前展示 " : ""}BNB Chain 参照数据 · four.meme / Backed / Binance · 新币采样时间 ${state.radar?.updatedAt ? new Date(state.radar.updatedAt).toLocaleString() : '尚未获取'}。名称匹配不表示官方关联或安全认证。`
    : (sourceText[okx.status] || 'X Layer 数据正在加载') + (okx.updatedAt ? ` 更新时间 ${new Date(okx.updatedAt).toLocaleString()}` : '');
  const metrics = [
    ['股票资产', bsc ? state.assets.length : (okx.status === 'ready' || okx.updatedAt ? okx.tokens.length : '—'), bsc ? 'BSC 参照范围' : 'OKX · X Layer'],
    ['新币', bsc && state.radar ? tokens.length : '—', bsc ? '当前窗口内最新 100 个' : 'Meme 发现源待接入'],
    ['股票名称线索', bsc && state.radar ? candidates.length : '—', '名称匹配后再做地址配对验证'],
    ['窗口交易笔数', bsc && state.radar ? state.radar.tradeCount : '—', bsc ? 'four.meme 买卖事件，非资金净流量' : '等待链上事件源'],
  ];
  document.getElementById('signalMetrics').innerHTML = metrics.map(([label, value, note]) => `<div class="kpi"><span class="kpi-label">${label}</span><strong class="kpi-value">${value}</strong><span class="kpi-note">${note}</span></div>`).join('');
  document.getElementById('liveCandidates').innerHTML = candidates.length ? candidates.slice(0, 6).map(candidateRow).join('') : '<div class="empty">当前网络暂无关联线索。可切换 BNB Chain 查看参照数据。</div>';
  document.getElementById('liveTokens').innerHTML = tokens.length ? tokens.slice(0, 6).map(x => `<a class="candidate" href="${detailLink(x.token)}"><strong>${e(x.symbol)}</strong><code>${shortAddress(x.token)}</code><span>${x.buys} 买 / ${x.sells} 卖 · ${x.volumeBnb != null ? fmtBnb(x.volumeBnb) : '成交额待采集'}</span><span>详情 →</span></a>`).join('') : '<div class="empty">' + (bsc ? '正在加载链上新币，首次连接可能需要稍候。' : 'X Layer Meme 发现源尚未接入。') + '</div>';
  document.getElementById('taxonomy').innerHTML = (state.relationTypes || []).map(r => `<div><strong>${e(r.label)}</strong><p>${e(r.evidence)}</p></div>`).join('');
  memeLegacy.hidden = !bsc;
  stockLegacy.hidden = !bsc;
  document.getElementById('memeUnavailable').hidden = bsc;
  document.getElementById('memeUnavailable').textContent = 'X Layer Meme 发现源尚未接入。RWA 股票列表不能替代 Meme 发现与关系验证。';
  const okxStocks = document.getElementById('okxStocks');
  okxStocks.hidden = bsc;
  okxStocks.innerHTML = okx.tokens.length ? `<div class="scroll"><table class="tbl"><thead><tr><th>代币 / 股票</th><th>CA</th><th>DEX 价</th><th>股票价</th><th>24h 成交额</th><th>发行方</th></tr></thead><tbody>${okx.tokens.map(a => `<tr><td>${e(a.tokenSymbol)} / ${e(a.stockCode)}</td><td><a href="${linkForToken(a.tokenContractAddress, '196')}" target="_blank" rel="noopener" title="${e(a.tokenContractAddress)}">${shortAddress(a.tokenContractAddress)} ↗</a></td><td>${money(a.price)}</td><td>${money(a.stockPrice)}</td><td>${money(a.volume24h)}</td><td>${e(a.issuer)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">暂无 X Layer 股票数据。连接 OKX 数据源后展示真实列表。</div>';
  document.getElementById('sectorValues').innerHTML = bsc && state.sectors?.length ? state.sectors.map(s => `<div class="candidate"><strong>${e(s.sector)}</strong><span>${s.value == null ? '—' : s.value.toFixed(2)}</span><span>${s.value == null ? e(s.reason) : `${s.members} 个已验证成分 · 基期 ${new Date(s.baseAt).toLocaleString()}`}</span></div>`).join('') : '等待已验证直接配对成分的价格与市值样本';
  renderDetail();
}
function money(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  const value = Number(n);
  const digits = Math.abs(value) > 0 && Math.abs(value) < 0.01 ? 8 : 4;
  return '$' + value.toLocaleString(undefined, { maximumFractionDigits: digits });
}
const legacyEnglish = {
  '查看资产与关系证据':'View assets and relationship evidence','输入合约地址，或从雷达列表中选择资产。':'Enter a contract address, or select an asset from a radar list.','合约地址':'Contract address','查看详情':'View details',
  '持续追踪':'Tracked','复制 CA':'Copy CA','区块浏览器':'Block explorer','创建者':'Creator','创建区块':'Created in block','时间未获取':'Time unavailable',
  '买入':'Buy','卖出':'Sell','累计成交额':'Cumulative volume','最新价格':'Latest price','暂无价格事件':'No price event','供应量':'Supply','暂无':'Unavailable','估算市值':'Estimated market cap','需要价格与供应量':'Price and supply required',
  '关系验证':'Relationship verification','直接配对 · 已核验地址':'Direct pair · address verified','名称线索 · 待验证':'Name lead · pending','尚未识别关系':'No relationship identified',
  '金库持仓：未采集':'Treasury holdings: not collected','回购 / 兑换证据：未采集':'Buyback / redemption evidence: not collected','欺诈检测：未检测，不能据此判定安全':'Fraud checks: not performed; safety cannot be inferred',
  '关联解读':'Relationship interpretation','规则说明 · 非 AI 生成':'Rule-based explanation · not AI generated','交易活跃度不能证明与股票存在资金联系。':'Trading activity does not prove a financial relationship with the stock.',
  '滚动相关系数':'Rolling correlation','股票捕获率':'Stock capture rate','Meme 热度':'Meme activity','AI 股票背景':'AI stock background','模型服务未接入':'Model service not connected',
  '联动价格 · 5 分钟采样':'Linked prices · 5-minute samples','名称对应的股票代币 · BSC 参照':'Stock tokens matching the name · BSC reference','等待已验证关系和独立价格样本':'Waiting for a verified relationship and independent price samples',
  '已复制':'Copied','请选中地址复制':'Select the address to copy'
};
function localizeLegacyText(el) {
  if (!el || !(typeof LANG !== 'undefined' && LANG === 'en')) return;
  el.innerHTML = Object.entries(legacyEnglish).reduce((html, [zh, en]) => html.split(zh).join(en), el.innerHTML);
}
let relationshipChart = null;
let detailFetchKey = '';
function renderDetail() {
  if (relationshipChart) { relationshipChart.dispose(); relationshipChart = null; }
  const [, chain, address] = location.hash.slice(1).split('/');
  const unifiedStock = state?.unified?.stockTokens?.some(token => token.tokenContractAddress?.toLowerCase() === String(address || '').toLowerCase());
  if ((chain === '196' || chain === '4663' || (chain === '56' && unifiedStock)) && window.XLayerUI) { window.XLayerUI.detail(address, chain); return; }
  delete document.getElementById('memeDetail').dataset.xAsset;
  if (!address) {
    document.getElementById('memeDetail').innerHTML = '<section class="panel detail-welcome"><h2>查看资产与关系证据</h2><p>输入合约地址，或从雷达列表中选择资产。</p><form id="xDetailSearch"><input name="address" required pattern="0x[0-9a-fA-F]{40}" placeholder="0x… 合约地址" aria-label="合约地址"><button>查看详情</button></form><p><a href="#meme">Meme Radar →</a>　<a href="#stock">Stock Links →</a></p></section>';
    return;
  }
  const addressKey = (address || '').toLowerCase();
  const token = chain === '56' && /^0x[\da-f]{40}$/i.test(address || '')
    ? (state?.radar?.tokens.find(x => x.token.toLowerCase() === addressKey)
      || state?.radar?.archiveTokens?.find(x => x.token.toLowerCase() === addressKey)
      || state?.leadCandidates?.find(x => x.token.toLowerCase() === addressKey))
    : null;
  const el = document.getElementById('memeDetail');
  if (!token) {
    if (chain === '56' && /^0x[\da-f]{40}$/i.test(address || '') && detailFetchKey !== addressKey) {
      detailFetchKey = addressKey;
      el.innerHTML = '<section class="panel empty"><h2>Meme Detail</h2><p>正在从已保存窗口补取该地址的链上记录…</p></section>';
      fetch(`/api/token/56/${addressKey}`).then(async response => {
        if (!response.ok) throw new Error('not found');
        return response.json();
      }).then(found => {
        state.radar = state.radar || { tokens: [], archiveTokens: [] };
        state.radar.archiveTokens = [found, ...(state.radar.archiveTokens || []).filter(x => x.token.toLowerCase() !== addressKey)];
        renderDetail();
      }).catch(() => {
        el.innerHTML = '<section class="panel empty"><h2>Meme Detail</h2><p>当前地址不在已保存的新币或线索记录中，历史补取也未找到可用事件。</p><a href="#meme">前往 Meme Radar →</a></section>';
      });
      return;
    }
    el.innerHTML = '<section class="panel empty"><h2>Meme Detail</h2><p>当前地址不在已保存的新币或线索记录中。返回 Meme Radar 选择代币。</p><a href="#meme">前往 Meme Radar →</a></section>'; return;
  }
  const r = token.relation;
  const verification = token.verification;
  const poolEvidence = verification?.pools?.length ? verification.pools.map(p => `<li>已验证直接配对：${e(p.stockSymbol)} · <a href="${linkForToken(p.pool)}" target="_blank" rel="noopener">${e(p.pool)}</a></li>`).join('') : `<li>配对池：${verification?.status === 'not_found' ? '限定的 BSC PancakeSwap V2 范围内未找到' : verification?.status === 'error' ? '查询失败，不能作出判断' : token.match ? '等待地址配对验证' : '未识别股票名称线索，未执行配对查询'}</li>`;
  const volume = token.volumeBnb != null ? fmtBnb(token.volumeBnb) : '暂无成交额事件';
  const marketCap = token.supplyMarketCapUsd ?? (token.supply > 0 && token.lastPriceUsd > 0 ? token.supply * token.lastPriceUsd : null);
  const activity = [
    ['买入', String(token.buys ?? 0)], ['卖出', String(token.sells ?? 0)], ['累计成交额', volume],
    ['最新价格', token.lastPriceUsd != null && token.lastPriceUsd > 0 ? money(token.lastPriceUsd) : '暂无价格事件'],
    ['供应量', token.supply ? fmtNum(token.supply, 0) : '暂无'], ['估算市值', marketCap ? money(marketCap) : '需要价格与供应量'],
  ];
  const matchingStocks = state.assets.filter(a => a.underlying?.toUpperCase() === token.match?.ticker);
  el.innerHTML = `<div class="page-heading"><a href="#meme">← Meme Radar</a><h2>${e(token.symbol)} <span class="tag">持续追踪</span></h2><p>${e(token.name)}</p></div>
  <section class="panel"><div class="address-row"><code>${e(token.token)}</code><button data-copy="${e(token.token)}">复制 CA</button><a href="${linkForToken(token.token)}" target="_blank" rel="noopener">区块浏览器 ↗</a></div><p>创建者 <code>${e(token.creator || '—')}</code></p><p>创建区块 ${token.block || '—'} · ${token.ts ? new Date(token.ts * 1000).toLocaleString() : '时间未获取'}</p></section>
  <section class="kpis activity-kpis">${activity.map(([label,value]) => `<div class="kpi"><span class="kpi-label">${label}</span><strong class="kpi-value">${e(value)}</strong></div>`).join('')}</section>
  <div class="cols main-cols"><section class="panel"><h2>关系验证</h2><p class="tag match">${verification?.status === 'verified' ? '直接配对 · 已核验地址' : token.match ? '名称线索 · 待验证' : '尚未识别关系'}</p><p>${e(verification?.status === 'verified' ? '已核验池内两侧代币地址。仅证明直接配对，不代表官方背书、足够流动性或合约安全。' : r?.explanation || '尚无验证结果')}</p><ul>${poolEvidence}<li>金库持仓：未采集</li><li>回购 / 兑换证据：未采集</li><li>欺诈检测：未检测，不能据此判定安全</li></ul><p class="hint">${e(verification?.scope || "待执行限定范围的 BSC 配对查询")}${verification?.checkedAt ? " · " + new Date(verification.checkedAt).toLocaleString() : ""}</p></section>
  <section class="panel"><h2>关联解读</h2><p class="hint">规则说明 · 非 AI 生成</p><p>${e(token.symbol)} 在当前窗口内有 ${token.buys} 笔买入、${token.sells} 笔卖出。${token.match ? `名称命中 ${e(token.match.ticker)}，仅能作为进一步核实的起点。` : '当前未命中股票名称词典。'}</p><p>交易活跃度不能证明与股票存在资金联系。</p></section></div>
  <div class="kpis">${[['滚动相关系数', token.analytics?.correlation?.reason || `4 小时 · ${token.analytics?.correlation?.samples ?? 0} 个对齐样本；相关不代表因果`], ['股票捕获率', '等待全量池流动性及已验证配对池'], ['Meme 热度', '等待成交量和持有人增速'], ['AI 股票背景', '模型服务未接入']].map(([label,note]) => `<div class="kpi"><span>${label}</span><strong class="kpi-value">${label === "滚动相关系数" && token.analytics?.correlation?.value != null ? token.analytics.correlation.value.toFixed(3) : "—"}</strong><p class="kpi-note">${e(note)}</p></div>`).join('')}</div>
  <section class="panel"><h2>联动价格 · 5 分钟采样</h2><p class="hint">仅对已验证直接配对且有独立价格池的代币采样；稳定币按 1 美元计。${token.analytics?.updatedAt ? "最近样本 " + new Date(token.analytics.updatedAt).toLocaleString() : "尚无样本"}</p><div id="relationshipChart" class="chart"></div></section>
  <section class="panel"><h2>名称对应的股票代币 · BSC 参照</h2>${matchingStocks.length ? matchingStocks.map(a => `<p>${e(a.symbol)} · ${e(a.name)} · 锚定价 ${money(a.oracle)} · DEX ${money(a.dexPrice)}</p>`).join('') : '<p>暂无同代码股票代币数据。</p>'}<p class="hint">此列表来自代码匹配，不构成 Meme 的持仓或配对证明。</p></section>`;
  localizeLegacyText(el);
  const chart = document.getElementById('relationshipChart');
  if (window.echarts && token.analytics?.prices?.length) {
    relationshipChart = echarts.init(chart);
    const normalize = pts => pts.map(p => [p.t, 100 * p.v / pts[0].v]);
    relationshipChart.setOption({tooltip:{trigger:'axis'},legend:{data:['Meme','股票'],textStyle:{color:'#91a0b5'}},xAxis:{type:'time'},yAxis:{type:'value',name:'首个样本 = 100'},series:[{name:'Meme',type:'line',showSymbol:false,data:normalize(token.analytics.prices)},{name:'股票',type:'line',showSymbol:false,data:normalize(token.analytics.stockPrices)}]});
  } else chart.innerHTML = '<div class="empty">等待已验证关系和独立价格样本</div>';
}
window.addEventListener('resize', () => relationshipChart?.resize());
function routeWorkspace() {
  const page = location.hash.slice(1).split('?')[0].split('/')[0] || 'live';
  const selected = ['live','meme','stock','detail',...(window.RadarV2?['pair','events']:[])].includes(page) ? page : 'live';
  if (!window.RadarV2 && selected === 'detail' && location.hash.includes('/56/')) network.value = '56';
  if (selected === 'detail' && (location.hash.includes('/196/') || location.hash.includes('/4663/'))) network.value = '196';
  for (const view of document.querySelectorAll('.view')) view.hidden = view.id !== 'view-' + selected;
  for (const a of document.querySelectorAll('.workspace-nav > a')) a.classList.toggle('active', a.getAttribute('href') === '#' + selected);
  renderWorkspace();
  requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
}
network.addEventListener('change', () => {
  networkInitialized = true;
  try { localStorage.setItem('radar-network', network.value); } catch {}
  if (location.hash.startsWith('#detail')) location.hash = '#meme';
  if (network.value === '56' && typeof renderAll === 'function' && state) renderAll();
  else renderWorkspace();
  window.dispatchEvent(new Event('resize'));
});
window.addEventListener('hashchange', routeWorkspace);
document.addEventListener('stateupdated', renderWorkspace);
document.addEventListener('languagechange', () => { applyWorkspaceLanguage(); renderWorkspace(); });
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-copy]');
  if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = workspaceText('已复制','Copied'); }
  catch { button.textContent = workspaceText('请选中地址复制','Select address to copy'); }
});
routeWorkspace();
