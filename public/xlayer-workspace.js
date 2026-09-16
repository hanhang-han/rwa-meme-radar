// Source-neutral relationship workflow. X Layer is the first connected source.
(() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const tr = (zh,en) => (typeof LANG !== 'undefined' && LANG === 'en') ? en : zh;
  const companyZh = { AAPL:'苹果', AMD:'超威半导体', AMAT:'应用材料', AMZN:'亚马逊', BABA:'阿里巴巴', COIN:'Coinbase', CRCL:'Circle', CRM:'赛富时', GME:'游戏驿站', GOOGL:'谷歌', HOOD:'Robinhood', INTC:'英特尔', META:'Meta', MSFT:'微软', MSTR:'Strategy', MU:'美光科技', NFLX:'奈飞', NVDA:'英伟达', ORCL:'甲骨文', PLTR:'帕兰提尔', PYPL:'贝宝', QCOM:'高通', QQQ:'纳斯达克100指数ETF', SNDK:'闪迪', SOXL:'半导体三倍做多ETF', SOXS:'半导体三倍做空ETF', SPY:'标普500指数ETF', TSLA:'特斯拉', TSM:'台积电' };
  const company = ticker => (typeof LANG === 'undefined' || LANG === 'zh') && companyZh[String(ticker||'').toUpperCase()] ? `${ticker} · ${companyZh[String(ticker).toUpperCase()]}` : ticker;
  const usd = v => v == null || !Number.isFinite(Number(v)) ? tr('待采集','Pending') : '$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:Number(v)>0&&Number(v)<0.01?8:2});
  const num = v => v == null ? tr('待采集','Pending') : Number(v).toLocaleString('en-US',{maximumFractionDigits:0});
  const date = v => v ? new Date(v).toLocaleString(typeof LANG !== 'undefined' && LANG==='en'?'en-US':'zh-CN',{hour12:false}) : tr('尚未采集','Not collected');
  const age = v => !v ? tr('待更新','Pending') : Date.now()-v<60000?tr('刚刚更新','Just updated'):tr(`${Math.floor((Date.now()-v)/60000)} 分钟前`,`${Math.floor((Date.now()-v)/60000)}m ago`);
  const short = a => a ? a.slice(0,7)+'…'+a.slice(-5) : '—';
  const link = (a,chain='196') => '#detail/'+encodeURIComponent(chain)+'/'+encodeURIComponent(a);
  const explorer = (a,type='address',chain='196') => chain==='4663'
    ? `https://robinhoodchain.blockscout.com/${type==='tx'?'tx':'address'}/${encodeURIComponent(a)}`
    : chain==='56'
      ? `https://www.oklink.com/bsc/${type}/${encodeURIComponent(a)}`
      : `https://www.oklink.com/xlayer/${type}/${encodeURIComponent(a)}`;
  const fresh = at => at && Date.now()-at<900000;
  const change = v => v == null ? tr('待采集','Pending') : `${v>=0?'+':''}${Number(v).toFixed(2)}%`;
  let snapshot, filter='related', search='', stockSearch='', page=0, stockPage=0, renderedLanguage;
  let lastTradeIds=new Set(), lastPriceText='';
  let detailCache = new Map(), detailPending = new Set(), detailChart;
  const sectors = ['芯片','交易所','支付','托管','加密国库'];
  const sectorName = value => ({'芯片':tr('芯片','Semiconductors'),'交易所':tr('交易所','Exchanges'),'支付':tr('支付','Payments'),'托管':tr('托管','Custody'),'加密国库':tr('加密国库','Crypto treasury')}[value]||value);
  const englishLiterals = {
    '芯片':'Semiconductors','交易所':'Exchanges','支付':'Payments','托管':'Custody','加密国库':'Crypto treasury',
    '包装股票配对':'Wrapped stock pair','直接股票配对':'Direct stock pair','地址已核验':'Address verified','证据变化':'Evidence changed','复核失败':'Review failed',
    '代币 ':'Token ','池 ':'Pool ','股票侧 ':'Stock side ','池总流动性 ':'Total pool liquidity ','核验区块 ':'Verified block ',
    '查看完整地址与核验依据':'View full addresses and verification basis','原始股票代币：':'Original stock token:','股票侧余额（原始最小单位）：':'Stock-side balance (base units):','未采集':'Not collected',
    '链上地址读取 ':'On-chain address read ','；池流动性为最近查询估值，不能视为锁仓证明。':'; pool liquidity is a recent queried valuation, not proof of locked value.',
    '价格观察 · 5 分钟采样':'Price observation · 5-minute samples','自开始追踪起的价格快照。至少两个采样点后显示曲线。':'Price snapshots since tracking began. The chart appears after two samples.',
    '已保存 ':'Saved ',' 个采样点，下一采样时段继续累积。':' samples; collection continues at the next interval.','股票背景与关联解读':'Stock background and relationship reading','规则说明 · AI 模型尚未接入':'Rules · AI model not connected',
    '发行方资料不代表与该 Meme 存在官方合作。':'Issuer information does not imply an official relationship with this Meme.','公司资料等待对应股票的背景信息返回。':'Company information is waiting for the underlying stock profile.',
    '股票捕获率：':'Stock capture rate:','最近成交':'Recent activity','保存并按成交 ID 去重':'Persisted and deduplicated by trade ID','时间':'Time','方向':'Side','美元成交额':'USD volume','成交价':'Trade price','交易':'Transaction',
    '买入':'Buy','卖出':'Sell','暂无交易哈希':'No transaction hash','当前已保存范围内暂无成交记录。':'No activity record exists in the saved range.',
    '成交采集排队中，缺失不表示零成交。':'Activity collection is queued; missing data does not mean zero trades.','检测到分页覆盖缺口，统计不完整。':'A pagination coverage gap was detected; statistics are incomplete.',
    '风险信息':'Risk information','风险等级：':'Risk level:','未提供':'Not provided','此次响应未提供风险标签':'This response does not include risk tags','检测时间 ':'Checked ',
    '风险数据尚未返回。':'Risk data has not returned yet.','关系时间线':'Relationship timeline','关系Time线':'Relationship timeline','等待首条关系核验事件；资产记录已持久保存。':'Waiting for the first relationship-verification event; the asset record is persisted.',
    '当前范围内暂无已核验股票关系。':'No verified stock relationship exists in the current scope.','尚未完成配对池核验。':'Pair-pool verification is not complete yet.','名称线索已保存。':' name lead saved.',
    '已核验股票配对':'Verified stock pair','Verified stock pair；请结合证据时效和资金规模判断。':'Verified stock pair; assess it with evidence freshness and capital scale.',
    '尚未验证独立股票现货行情与独立 Meme 价格，避免配对计价造成机械相关':'Independent stock spot price and Meme price are not yet verified; paired pricing is excluded to avoid mechanical correlation.',
    '当前仅前 5 池，缺少同口径股票侧流动性分母':'Only the top five pools are covered; the comparable stock-side liquidity denominator is missing.',
    '当前已保存最近 24h 内 ':'Saved in the latest 24h: ',' 笔：':' trades: ',' 买 / ':' buys / ',' 卖。采集范围从 ':' sells. Collection starts at ',' 起；不是全历史统计。':'; this is not all-history data.',
    'Top 10 持仓':'Top 10 holdings','检测Time':'Checked ','OKX 风险标签为辅助信息；配对核验不代表安全认证。':'OKX risk tags are supplementary; pair verification is not a safety certification.',
    '已核验':'Verified','样本未齐':'Insufficient samples','分母未齐':'Incomplete denominator','检测未完整':'Checks are incomplete'
  };
  const localizeRenderedText = el => {
    if(typeof LANG === 'undefined' || LANG !== 'en' || !el) return;
    el.innerHTML = Object.entries(englishLiterals).reduce((html,[zh,en]) => html.split(zh).join(en),el.innerHTML);
  };
  const labels = {stock:()=>tr('股票代币','Stock token'),wrapped_stock:()=>tr('包装股票','Wrapped stock'),quote:()=>tr('基础资产','Base asset'),candidate:()=>tr('待分类代币','Unclassified token')};
  const feed = () => snapshot?.unified || snapshot?.xlayer || {};
  const relationFor = token => (feed().relations||[]).filter(r=>r.token===token);
  const activeRelations = token => relationFor(token).filter(r=>r.status==='verified'&&Date.now()-r.checkedAt<3600000);
  function openState(el) { return new Set([...el.querySelectorAll('details[open][data-open-id]')].map(d=>d.dataset.openId)); }
  function restoreOpen(el,ids) { el.querySelectorAll('details[data-open-id]').forEach(d=>{d.open=ids.has(d.dataset.openId);}); }
  function badge(asset) {
    const rel=activeRelations(asset.token);
    if(rel.length) return `<span class="x-badge verified">${tr('配对已核验','Verified pair')} · ${esc([...new Set(rel.map(r=>r.ticker))].join(' / '))}</span>`;
    if(relationFor(asset.token).length) return `<span class="x-badge pending">${tr('历史证据 · 待复核','Historical evidence · needs review')}</span>`;
    return asset.match?`<span class="x-badge pending">${esc(asset.match.ticker)} ${tr('名称线索','name lead')}</span>`:`<span class="x-badge">${tr('未识别股票关系','No stock relation')}</span>`;
  }
  const kpi = (label,value,note,attrs='') => `<div class="kpi" ${attrs}><span class="kpi-label">${label}</span><strong class="kpi-value">${value}</strong><span class="kpi-note">${note}</span></div>`;
  function candidate(a) {
    return `<a class="x-signal" href="${link(a.token)}"><div><strong>${esc(a.symbol)}</strong> ${badge(a)}<small>${esc(a.name)} · ${esc(short(a.token))} · ${tr('来源','Source')} ${esc(a.provider||tr('已接入目录','Connected catalogue'))}</small></div><div><strong>${usd(a.volume24h)}</strong><small>${tr('24h 成交额','24h volume')} · ${age(a.updatedAt)}</small></div><span aria-hidden="true">↗</span></a>`;
  }
  function ensure() {
    if(!document.getElementById('xlayerLive')) {
      const el=document.createElement('div');el.id='xlayerLive';document.getElementById('view-live').append(el);
    }
    if(!document.getElementById('xlayerMeme')) {
      const el=document.createElement('div');el.id='xlayerMeme';document.getElementById('view-meme').append(el);
    }
  }
  function render(s) {
    snapshot=s;ensure();
    const selectedPage=location.hash.slice(1).split('/')[0]||'live';
    const currentLanguage=typeof LANG !== 'undefined' ? LANG : 'zh';
    if(renderedLanguage && renderedLanguage!==currentLanguage) {
      document.getElementById('xlayerMeme').innerHTML='';
      document.getElementById('okxStocks').innerHTML='';
    }
    renderedLanguage=currentLanguage;
    document.getElementById('xlayerLive').hidden=false;document.getElementById('xlayerMeme').hidden=false;
    const x=s.unified||s.xlayer||{}, c=x.coverage||{}, metrics=x.metrics||{}, assets=x.assets||[];
    const related=assets.filter(a=>a.kind==='candidate'&&activeRelations(a.token).some(r=>(r.liquidityUsd||0)>=1000)).sort((a,b)=>(b.volume24h||0)-(a.volume24h||0));
    const sources=x.sources||[];
    const ready=sources.filter(source=>source.status==='ready').length;
    const delayed=sources.some(source=>source.status==='stale'||source.status==='error');
    const sourceLabels=sources.map(source=>`${esc(source.provider||tr('目录','Catalogue'))}${tr('（','(')}${source.status==='ready'?tr('已更新','updated'):source.status==='stale'?tr('延迟','delayed'):tr('连接中','connecting')}${tr('）',')')}`).join(' · ');
    document.getElementById('sourceStatus').textContent=`${tr('已接入数据源','Data sources')}${tr('：',':')} ${sourceLabels} · ${tr('股票资产','Stock assets')} ${num((x.stockTokens||[]).length||c.catalog)} · ${tr('已扫描','Scanned')} ${num(c.scanned)} · ${x.running?tr('正在核验下一批','Verifying next batch'):age(x.updatedAt)}${delayed?tr(' · 部分目录更新延迟',' · Some catalogues are delayed'):''}${x.error?' · '+x.error:''}`;
    if(selectedPage==='live') {
    const liveOpen=openState(document.getElementById('xlayerLive'));
    document.getElementById('xlayerLive').innerHTML=`
      <div class="hero x-hero"><div><span class="eyebrow">RELATIONSHIP INTELLIGENCE</span><h2>${tr('关系，从链上证据开始。','Relationships start with on-chain evidence.')}</h2><p>${tr('从股票资产反查配对，再看成交与资金规模。每条结论都可追溯到可核验的链上证据。','Trace pairs from stock assets, then examine trading and capital. Every conclusion links to verifiable on-chain evidence.')}</p></div><a class="x-button" href="#meme">${tr('查看关系雷达','Open relation radar')} ↗</a></div>
      <div class="kpis">${kpi(tr('重点关联资产','Priority related assets'),num(metrics.actionableAssets),tr(`已核验共 ${num(metrics.verifiedAssets)} 个 · 重点池 ≥ $1,000`,`Verified ${num(metrics.verifiedAssets)} · priority pools ≥ $1,000`))}${kpi(tr('已核验配对池','Verified pair pools'),num(metrics.verifiedPools),tr('包括链上验证的包装股票','Includes verified wrapped stocks'))}${kpi(tr('已覆盖配对池流动性','Covered pair liquidity'),usd(metrics.pairedLiquidityUsd),tr(`${num(metrics.liquidityCoverage?.valued)} / ${num(metrics.verifiedPools)} 池有新鲜估值 · 非锁仓`,`${num(metrics.liquidityCoverage?.valued)} / ${num(metrics.verifiedPools)} pools have fresh valuations · not locked value`))}${kpi('Stock Flow',tr('待采集','Pending'),tr('LP 流入 / 流出事件尚未归集','LP inflow / outflow events not yet indexed'))}</div>
      <div class="x-grid"><section class="panel"><div class="panel-head"><h2>${tr('关系事件','Relationship events')}</h2><span class="hint">${tr('首次核验与证据变化 · 持久保存','First verification and evidence changes · persisted')}</span></div>
      ${(x.signals||[]).length?(x.signals||[]).slice(0,8).map(event=>{const rel=(x.relations||[]).find(r=>r.id.startsWith(`${event.id.split(':').slice(0,4).join(':')}`));return `<a class="x-event" href="${link(event.asset)}"><span class="x-event-dot"></span><div><strong>${esc(event.symbol||short(event.asset))} → ${esc(event.ticker)}</strong><p>${esc(event.label)}</p><small>${tr('首次记录','First recorded')} ${date(event.t)} · ${tr('最近复核','Last reviewed')} ${rel?date(rel.checkedAt):tr('待复核','Pending')} · ${esc(short(event.pool))}</small></div><span>${tr('查看证据','View evidence')} ↗</span></a>`}).join(''):`<div class="x-empty">${tr('正在反查股票配对池。发现并核验关系后，事件会出现在这里。','Tracing stock pair pools. Events appear here once a relationship is found and verified.')}</div>`}</section>
      <section class="panel"><div class="panel-head"><h2>${tr('行业篮子','Sector baskets')}</h2><span class="hint">${tr('固定基期 100 · 合格成分 ≥ 3','Fixed base 100 · at least 3 qualified members')}</span></div>${(x.sectors||sectors.map(sector=>({sector,members:0,reason:tr('等待合格成分','Waiting for qualified members')}))).map(b=>`<details class="x-basket"><summary><span>${esc(b.sector)}</span><strong>${b.value==null?`${b.members}/3 ${tr('成分','members')}`:Number(b.value).toFixed(2)}</strong></summary><p>${esc(b.reason)}</p>${(b.components||[]).map(a=>`<p><a href="${link(a.token)}">${esc(a.symbol)}</a>${a.weight!=null?' · '+(a.weight*100).toFixed(1)+'%':''}</p>`).join('')}${b.baseAt?`<small>${tr('基期','Base')} ${date(b.baseAt)}</small>`:''}</details>`).join('')}<p class="hint">${tr('股票直接/包装配对且池流动性 ≥ $1,000；行情有效。名称候选不计入。','Direct or wrapped stock pairs with pool liquidity ≥ $1,000 and valid prices; name leads are excluded.')}</p></section></div>
      <section class="panel"><div class="panel-head"><h2>${tr('已核验关系 · 按交易活动排序','Verified relationships · sorted by activity')}</h2><a href="#meme">${tr('全部关系','All relationships')} →</a></div>${related.length?related.slice(0,6).map(candidate).join(''):`<p class="x-empty">${tr('尚无已完成核验的关系。扫描进度持续更新，原始名称线索可在 Meme Radar 查看。','No relationship has completed verification yet. Scanning continues; original name leads are available in Meme Radar.')}</p>`}</section>
      <div class="x-grid"><section class="panel"><h2>${tr('热度输入覆盖','Activity-input coverage')}</h2><p>${tr('交易量与持有人快照已开始采集，股票实际锁定量与同比样本尚未齐备。','Volume and holder snapshots are being collected. Stock amount locked and comparison samples are not complete yet.')}</p><div class="x-coverage"><span>${tr('成交额','Volume')} ${assets.filter(a=>a.kind==='candidate'&&a.volume24h!=null).length} ${tr('个','assets')}</span><span>${tr('持有人','Holders')} ${assets.filter(a=>a.kind==='candidate'&&a.holders!=null).length} ${tr('个','assets')}</span><span>${tr('完整热度：待形成','Full activity score: pending')}</span></div></section><details class="panel"><summary>${tr('发现策略与覆盖范围','Discovery strategy and coverage')}</summary><p>${esc(c.scope||tr('正在初始化','Initializing'))}</p><p>${tr(`已读取 ${num(c.pools)} 个池；${num(c.errors)} 个资产的查询存在未覆盖接口或错误。流动性前五池不是全量覆盖。`,`Read ${num(c.pools)} pools; ${num(c.errors)} asset queries have unsupported endpoints or errors. The five most liquid pools are not full coverage.`)}</p><p>${tr('配对与包装映射通过链上读取核验；资金规模来自已接入行情目录。新币/热门榜单仅作为补充发现。','Pairs and wrapper mappings are verified from on-chain reads; market size comes from connected catalogues. New-token and trending lists are discovery inputs only.')}</p></details></div>`;
    document.querySelectorAll('#xlayerLive .x-basket').forEach((d,i)=>d.dataset.openId='basket-'+i);
    restoreOpen(document.getElementById('xlayerLive'),liveOpen);
    localizeRenderedText(document.getElementById('xlayerLive'));
    }
    if(selectedPage==='meme') renderMeme();
    if(selectedPage==='stock') renderStocks();
  }
  function assetRow(a) {
    const split=fresh(a.countsAt)?`${num(a.buys24h)} / ${num(a.sells24h)}`:tr('分项待采集','Breakdown pending');
    return `<tr><td><a href="${link(a.token,a.chainId||a.chain||'196')}"><strong>${esc(a.symbol)}</strong></a><small>${esc(short(a.token))} · ${tr('来源','Source')} ${esc(a.provider||tr('已接入目录','Connected catalogue'))}</small></td><td>${badge(a)}</td><td>${usd(a.price)}<small class="${(a.change24h||0)>=0?'up':'down'}">${change(a.change24h)}</small></td><td>${usd(a.volume24h)}</td><td>${num(a.txs24h)}<small>${tr('买 / 卖','Buy / sell')}: ${split}</small></td><td>${usd(a.liquidity)}</td><td>${num(a.holders)}</td><td>${age(a.updatedAt)}<small>${a.error?tr('部分更新失败','Partial update failed'):fresh(a.updatedAt)?tr('当前快照','Current snapshot'):tr('历史样本','Historical sample')}</small></td></tr>`;
  }
  function renderMeme() {
    const el=document.getElementById('xlayerMeme');if(!el||!snapshot)return;
    const x=feed();
    // Keep the input node stable across refreshes to preserve focus and caret.
    if(!document.getElementById('xMemeRows')) el.innerHTML=`<section class="panel"><div class="x-toolbar"><div class="tab-group" id="xMemeFilters">${[['related',tr('重点关系','Priority')],['verified',tr('全部已核验','All verified')],['name',tr('名称候选','Name leads')],['all',tr('全部发现','All discoveries')],['history',tr('历史记录','History')]].map(([id,label])=>`<button class="tab" data-x-filter="${id}">${label}</button>`).join('')}</div><input id="xMemeSearch" placeholder="${tr('搜索名称、股票或 CA','Search name, stock or CA')}" aria-label="${tr('搜索关联资产','Search related assets')}"></div><p class="hint">${tr('重点关系：已核验且配对池流动性 ≥ $1,000。低流动性证据保留在“全部已核验”。同名折叠，每个 CA 独立保存；交易活跃不代表安全。','Priority: verified pairs with pool liquidity ≥ $1,000. Low-liquidity evidence remains under All verified. Same-name contracts are grouped, but every CA is retained; activity does not prove safety.')}</p><div id="xMemeRows"></div><div class="x-pager"><button data-x-page="-1">${tr('上一页','Previous')}</button><span id="xMemePage"></span><button data-x-page="1">${tr('下一页','Next')}</button></div></section>`;
    document.querySelectorAll('[data-x-filter]').forEach(b=>b.classList.toggle('active',b.dataset.xFilter===filter));
    let groups=(x.groups||[]).map(g=>({...g,members:g.members.filter(a=>{
      const term=[a.name,a.symbol,a.token,a.match?.ticker,...relationFor(a.token).map(r=>r.ticker)].join(' ').toLowerCase();
      if(search&&!term.includes(search.toLowerCase()))return false;
      if(filter==='related')return activeRelations(a.token).some(r=>(r.liquidityUsd||0)>=1000);
      if(filter==='verified')return activeRelations(a.token).length>0;
      if(filter==='name')return !!a.match&&!activeRelations(a.token).length;
      if(filter==='history')return !fresh(a.updatedAt);
      return true;
    })})).filter(g=>g.members.length);
    const pages=Math.max(1,Math.ceil(groups.length/15));page=Math.min(page,pages-1);
    const head=`<thead><tr><th>${tr('资产 / CA','Asset / CA')}</th><th>${tr('关系','Relation')}</th><th>${tr('价格 / 24h','Price / 24h')}</th><th>${tr('24h 成交额','24h volume')}</th><th>${tr('24h 交易','24h trades')}</th><th>${tr('流动性','Liquidity')}</th><th>${tr('持有人','Holders')}</th><th>${tr('数据时效','Freshness')}</th></tr></thead>`;
    const groupOpen=openState(document.getElementById('xMemeRows'));
    document.getElementById('xMemeRows').innerHTML=groups.length?groups.slice(page*15,page*15+15).map(g=>g.members.length===1?`<div class="scroll"><table class="tbl x-table">${head}<tbody>${assetRow(g.members[0])}</tbody></table></div>`:`<details class="x-group" data-open-id="${esc(g.symbol)}"><summary><strong>${esc(g.symbol)}</strong><span>${g.members.length} ${tr('个独立合约 · 展开比较','independent contracts · compare')}</span></summary><div class="scroll"><table class="tbl x-table">${head}<tbody>${g.members.map(assetRow).join('')}</tbody></table></div></details>`).join(''):`<div class="x-empty">${tr('当前筛选暂无资产。可切换“全部发现”查看热门榜和已发现的股票配对候选。','No assets match this filter. Switch to All discoveries for the hot list and discovered stock-pair candidates.')}</div>`;
    restoreOpen(document.getElementById('xMemeRows'),groupOpen);
    document.getElementById('xMemePage').textContent=`${page+1} / ${pages} · ${groups.length} ${tr('组','groups')}`;
  }
  function renderStocks() {
    const el=document.getElementById('okxStocks');if(!el||!snapshot)return;
    if(!document.getElementById('xStockRows')) el.innerHTML=`<div class="x-toolbar"><h2>${tr('股票 → 链上资产 → 关联代币','Stocks → on-chain assets → related tokens')}</h2><input id="xStockSearch" placeholder="${tr('搜索股票代码、名称、CA','Search ticker, name or CA')}" aria-label="${tr('搜索股票','Search stocks')}"></div><p class="hint">${tr('按股票代码聚合发行方与合约。股票现货价缺失时保留原始空值；点击可查看资产与关系证据。','Grouped by ticker across issuers and contracts. Missing spot prices remain unknown; open a row for assets and relationship evidence.')}</p><div id="xStockRows"></div><div class="x-pager"><button data-x-stock-page="-1">${tr('上一页','Previous')}</button><span id="xStockPage"></span><button data-x-stock-page="1">${tr('下一页','Next')}</button></div>`;
    const grouped=new Map();
    for(const stock of feed().stockTokens||snapshot.okx?.tokens||[]) {
      const key=stock.stockCode||stock.tokenContractAddress||stock.instrumentId;
      if(stockSearch && ![stock.stockCode,stock.tokenSymbol,stock.tokenName,stock.tokenContractAddress].join(' ').toLowerCase().includes(stockSearch.toLowerCase())) continue;
      grouped.set(key,[...(grouped.get(key)||[]),stock]);
    }
    const rows=[...grouped.entries()].sort((a,b)=>b[1].reduce((n,s)=>n+(s.volume24h||0),0)-a[1].reduce((n,s)=>n+(s.volume24h||0),0));
    const pages=Math.max(1,Math.ceil(rows.length/20));stockPage=Math.min(stockPage,pages-1);
    const stockOpen=openState(document.getElementById('xStockRows'));
    document.getElementById('xStockRows').innerHTML=rows.slice(stockPage*20,stockPage*20+20).map(([ticker,list])=>{
      const relations=(feed().relations||[]).filter(r=>list.some(s=>s.tokenContractAddress?.toLowerCase()===r.stock));
      const count=new Set(relations.filter(r=>r.status==='verified').map(r=>r.token)).size;
      return `<details class="x-stock"><summary><strong>${esc(company(ticker))}</strong><span>${list.length} ${tr('个股票资产','stock assets')}</span><span>${count} ${tr('个关联资产','related assets')}</span><span>24h ${usd(list.reduce((n,s)=>n+(s.volume24h||0),0))}</span></summary><div class="scroll"><table class="tbl"><thead><tr><th>${tr('股票资产','Stock asset')}</th><th>${tr('发行方','Issuer')}</th><th>${tr('代币价格','Token price')}</th><th>${tr('标的参考价','Underlying reference')}</th><th>${tr('24h 成交额','24h volume')}</th><th>${tr('详情','Details')}</th></tr></thead><tbody>${list.map(s=>`<tr><td>${esc(s.tokenSymbol)}<small>${s.tokenContractAddress?esc(short(s.tokenContractAddress)):tr('场内目录资产','Exchange catalogue asset')} · ${tr('来源','Source')} ${esc(s.provider||tr('已接入目录','Connected catalogue'))}</small></td><td>${esc(s.issuer)}</td><td>${usd(s.price)}</td><td>${s.stockPrice==null?tr('上游未提供','Not provided'):usd(s.stockPrice)}</td><td>${s.volumeScope==='exchange'?`${tr('场内','Exchange')} ${usd(s.volume24h)}`:usd(s.volume24h)}</td><td>${s.tokenContractAddress?`<a href="${link(s.tokenContractAddress,s.chainId||s.chainIndex||'196')}">${tr('行情与证据','Market & evidence')} ↗</a>`:tr('目录资料','Catalogue only')}</td></tr>`).join('')}</tbody></table></div>${relations.map(r=>`<p><a href="${link(r.token,r.chainId||'196')}">${esc((feed().assets||[]).find(a=>a.token===r.token)?.symbol||short(r.token))}</a> · ${r.wrapper?tr('包装股票配对','Wrapped stock pair'):tr('直接配对','Direct pair')} · ${age(r.checkedAt)}</p>`).join('')}</details>`;
    }).join('')||`<div class="x-empty">${tr('暂无匹配股票。','No matching stocks.')}</div>`;
    document.querySelectorAll('#xStockRows .x-stock').forEach(d=>d.dataset.openId=d.querySelector('summary strong').textContent);
    restoreOpen(document.getElementById('xStockRows'),stockOpen);
    document.getElementById('xStockPage').textContent=`${stockPage+1} / ${pages} · ${rows.length} ${tr('个股票标的','stock underlyings')}`;
  }
  async function detail(address,chain='196') {
    if(!/^0x[\da-f]{40}$/i.test(address||''))return;
    const key=address.toLowerCase(),cacheKey=`${chain}:${key}`,cached=detailCache.get(cacheKey);
    if(cached) drawDetail(cached.data);
    if(cached&&Date.now()-cached.at<30000||detailPending.has(cacheKey))return;
    detailPending.add(cacheKey);
    if(!cached)document.getElementById('memeDetail').innerHTML=`<section class="panel x-empty">${tr('正在读取已保存的资产、成交与证据…','Loading saved assets, activity and evidence…')}</section>`;
    try {
      const res=await fetch('/api/token/'+encodeURIComponent(chain)+'/'+key);
      if(!res.ok)throw new Error(res.status===404?tr('该 CA 尚未进入追踪索引；可从股票或关系雷达选择已发现资产。','This CA is not in the tracking index; select a discovered asset from Stock or Meme Radar.'):tr('数据读取失败，请稍后重试。','Could not load data. Please retry.'));
      const data=await res.json();detailCache.set(cacheKey,{data,at:Date.now()});
      if(location.hash.toLowerCase()==='#detail/'+chain+'/'+key)drawDetail(data);
    }catch(error){if(location.hash.toLowerCase()==='#detail/'+chain+'/'+key)document.getElementById('memeDetail').innerHTML=`<section class="panel x-empty">${esc(error.message)}<p><a href="#meme">${tr('返回关系雷达','Back to Meme Radar')} →</a></p></section>`;}
    finally{detailPending.delete(cacheKey);}
  }
  const tradeRow = (t,a) => `<tr><td>${date(t.t)}</td><td class="${t.type==='buy'?'up':'down'}">${t.type==='buy'?'买入':'卖出'}</td><td>${usd(t.volume)}</td><td>${usd(t.price)}</td><td>${esc(t.dex)}</td><td>${t.hash?`<a href="${explorer(t.hash,'tx',a.chainId||a.chain||'196')}" target="_blank" rel="noopener">${esc(short(t.hash))} ↗</a>`:'暂无交易哈希'}</td></tr>`;
  const tradeStat = (a,d) => a.tradeAt?`当前已保存最近 24h 内 ${d.activity.count} 笔：${d.activity.buys} 买 / ${d.activity.sells} 卖。采集范围从 ${date(a.oldestTradeAt)} 起；不是全历史统计。`:'成交采集排队中，缺失不表示零成交。';
  // Incremental refresh for the same asset: append chart points, slide in new
  // trades, flash the price KPI. The surrounding DOM is never rebuilt.
  function updateDetail(d) {
    const a=d.asset;
    if(detailChart&&document.getElementById('xPriceChart')&&window.echarts){
      const data=d.samples.map(s=>[s.t,s.price]);
      if(a.price!=null)data.push([Date.now(),a.price]);
      detailChart.setOption({series:[{data}]});
    }
    const tbody=document.getElementById('xTradeBody');
    if(tbody){
      const fresh=d.trades.filter(t=>!lastTradeIds.has(t.id));
      if(fresh.length){
        tbody.insertAdjacentHTML('afterbegin',fresh.map(t=>`<tr class="trade-new">${tradeRow(t,a).slice(3)}`).join(''));
        while(tbody.children.length>150)tbody.lastElementChild.remove();
      }
    }
    d.trades.forEach(t=>lastTradeIds.add(t.id));
    const stat=document.getElementById('xTradeStat');
    if(stat&&a.tradeAt)stat.textContent=tradeStat(a,d);
    const priceEl=document.querySelector('#memeDetail [data-kpi="price"] .kpi-value');
    if(priceEl&&a.price!=null){
      const text=usd(a.price);
      if(text!==lastPriceText){
        priceEl.textContent=text;
        priceEl.classList.remove('kpi-flash');void priceEl.offsetWidth;priceEl.classList.add('kpi-flash');
      }
      lastPriceText=text;
    }
  }
  function drawDetail(d) {
    const a=d.asset,relations=d.relations||[], stock=d.stock;
    if(document.getElementById('memeDetail').dataset.xAsset===a.token){updateDetail(d);return;}
    if(detailChart){detailChart.dispose();detailChart=null;}
    document.getElementById('memeDetail').dataset.xAsset=a.token;
    const countText=fresh(a.countsAt)?`${num(a.buys24h)} ${tr('买','buys')} / ${num(a.sells24h)} ${tr('卖','sells')}`:tr('买卖分项见已采集成交','Buy/sell detail appears in collected activity');
    const profile=a.profile||(d.stocks||[]).find(s=>s.profile)?.profile;
    document.getElementById('memeDetail').innerHTML=`
      <div class="page-heading"><a href="${stock?'#stock':'#meme'}">← ${stock?tr('股票','Stock'):tr('Meme','Meme')} Radar</a><h2>${esc(a.symbol)} <span class="x-badge">${tr('持续追踪','Tracked')}</span> <span class="x-badge">${tr('来源','Source')} ${esc(a.provider||tr('已接入目录','Connected catalogue'))}</span></h2><p>${esc(stock?company(a.stockCode||a.symbol):a.name)} · ${esc(labels[a.kind]?.()||tr('资产','Asset'))} · ${tr('首次发现','First seen')} ${date(a.firstSeen)}</p></div>
      <section class="panel"><div class="address-row"><code>${esc(a.token)}</code><button data-copy="${esc(a.token)}">${tr('复制 CA','Copy CA')}</button><a href="${explorer(a.token,'address',a.chainId||a.chain||'196')}" target="_blank" rel="noopener">${tr('区块浏览器','Block explorer')} ↗</a></div><p class="hint">${tr('行情','Market')} ${date(a.updatedAt)} · ${tr('成交','Activity')} ${date(a.tradeAt)}${a.error?' · '+esc(a.error):''}</p></section>
      <div class="kpis">${kpi(tr('最新价格','Latest price'),usd(a.price),change(a.change24h)+' · 24h','data-kpi="price"')}${kpi(tr('24h 成交额','24h volume'),usd(a.volume24h),a.volumeScope==='exchange'?tr('场内行情统计','Exchange market statistics'):tr('已采集行情统计','Collected market statistics'))}${kpi(tr('24h 交易','24h trades'),num(a.txs24h),countText)}${kpi(tr('流动性','Liquidity'),usd(a.liquidity),num(a.holders)+' '+tr('持有人','holders'))}</div>
      <section class="panel x-verdict"><span class="eyebrow">RELATIONSHIP EVIDENCE</span><h2>${stock?tr('股票代币与关联资产','Stock token and related assets'):esc(d.analysis.conclusion)}</h2><p>${stock?tr('该股票代币已进入资产目录，下面展示已核验的配对关系。','This stock token is in the asset catalogue. Verified pair relationships appear below.'):tr('配对池和股票包装映射分别核验；名称匹配只作为发现起点。','Pair pools and wrapped-stock mappings are verified separately; name matching is only a discovery starting point.')}</p>
      ${relations.length?relations.map(r=>`<article class="x-proof"><div class="panel-head"><h3>${esc(r.ticker)} · ${r.wrapper?'包装股票配对':'直接股票配对'}</h3><span class="x-badge ${r.status==='verified'?'verified':'pending'}">${r.status==='verified'?'地址已核验':r.status==='invalid'?'证据变化':'复核失败'} · ${age(r.checkedAt)}</span></div><p>代币 <a href="${link(r.token,r.chainId||a.chainId||'196')}">${esc(short(r.token))}</a> → 池 <a href="${explorer(r.pool,'address',r.chainId||a.chainId||'196')}" target="_blank" rel="noopener">${esc(short(r.pool))}</a> → 股票侧 <a href="${explorer(r.stockSide,'address',r.chainId||a.chainId||'196')}" target="_blank" rel="noopener">${esc(short(r.stockSide))}</a>${r.wrapper?` → asset()/underlying() → <a href="${link(r.stock,r.chainId||a.chainId||'196')}">${esc(short(r.stock))}</a>`:''}</p><div class="x-coverage"><span>${esc(r.protocol)}</span><span>池总流动性 ${usd(r.liquidityUsd)}</span><span>核验区块 ${num(r.block)}</span></div><details><summary>查看完整地址与核验依据</summary><p>token0：<code>${esc(r.token0)}</code></p><p>token1：<code>${esc(r.token1)}</code></p><p>原始股票代币：<code>${esc(r.stock)}</code></p><p>股票侧余额（原始最小单位）：${esc(r.stockBalance??'未采集')}</p><p>链上地址读取 ${date(r.checkedAt)}；池流动性为最近查询估值，不能视为锁仓证明。</p></details></article>`).join(''):`<div class="x-empty">${a.match?`${esc(a.match.ticker)} 名称线索已保存。`:''}${d.scan?`已查询 ${d.scan.poolCount} 个高流动性池，状态 ${esc(d.scan.status)}。`:'尚未完成配对池核验。'}当前范围内暂无已核验股票关系。</div>`}</section>
      <div class="x-grid"><section class="panel"><h2>价格观察 · 5 分钟采样</h2><p class="hint">自开始追踪起的价格快照。至少两个采样点后显示曲线。</p>${d.samples.length>=2?'<div id="xPriceChart" class="chart"></div>':`<div class="x-empty">已保存 ${d.samples.length} 个采样点，下一采样时段继续累积。</div>`}</section><section class="panel"><h2>股票背景与关联解读</h2><p class="hint">规则说明 · AI 模型尚未接入</p>${profile?`<h3>${esc(profile.companyName)}</h3><p>${esc(profile.exchange)} · ${esc(profile.industry)} · ${esc(profile.stockCode)}</p><p>发行方资料不代表与该 Meme 存在官方合作。</p>`:'<p>公司资料等待对应股票的背景信息返回。</p>'}<p>${esc(d.analysis.conclusion)}</p><p>${esc(d.analysis.correlation.reason)}</p><p>股票捕获率：${esc(d.analysis.capture.reason)}</p></section></div>
      <section class="panel"><div class="panel-head"><h2>最近成交</h2><span class="hint">保存并按成交 ID 去重</span></div><p class="hint" id="xTradeStat">${tradeStat(a,d)}${a.gapDetected?' 检测到分页覆盖缺口，统计不完整。':''}</p>${d.trades.length?`<div class="scroll"><table class="tbl"><thead><tr><th>时间</th><th>方向</th><th>美元成交额</th><th>成交价</th><th>DEX</th><th>交易</th></tr></thead><tbody id="xTradeBody">${d.trades.map(t=>tradeRow(t,a)).join('')}</tbody></table></div>`:'<div class="x-empty">当前已保存范围内暂无成交记录。</div>'}</section>
      <div class="x-grid"><section class="panel"><h2>风险信息</h2>${a.risk?`<p>风险等级：${esc(a.risk.level)} / 5 · Top 10 持仓 ${a.risk.top10==null?'未提供':a.risk.top10+'%'}</p><p>${a.risk.tags.length?a.risk.tags.map(tag=>`<span class="x-badge">${esc(tag)}</span>`).join(' '):'此次响应未提供风险标签'}</p><p class="hint">检测时间 ${date(a.risk.checkedAt)}</p>`:'<p>风险数据尚未返回。</p>'}<p>${esc(d.analysis.safety)}</p></section><section class="panel"><h2>关系时间线</h2>${d.events.length?d.events.map(event=>`<p>${date(event.t)} · ${esc(event.label)}</p>`).join(''):'<p>等待首条关系核验事件；资产记录已持久保存。</p>'}</section></div>`;
    localizeRenderedText(document.getElementById('memeDetail'));
    lastTradeIds=new Set(d.trades.map(t=>t.id));lastPriceText=a.price!=null?usd(a.price):'';
    if(d.samples.length>=2 && window.echarts) {
      const data=d.samples.map(s=>[s.t,s.price]);
      if(a.price!=null)data.push([Date.now(),a.price]);
      detailChart=echarts.init(document.getElementById('xPriceChart'));
      detailChart.setOption({tooltip:{trigger:'axis'},grid:{left:70,right:20,top:20,bottom:35},xAxis:{type:'time',axisLabel:{color:'#91a0b5'}},yAxis:{type:'value',scale:true,axisLabel:{color:'#91a0b5'},splitLine:{lineStyle:{color:'#253044'}}},series:[{type:'line',showSymbol:false,data,lineStyle:{color:'#b9fa6a'},areaStyle:{color:'rgba(185,250,106,.06)'}}]});
    }
  }
  document.addEventListener('click',event=>{
    const f=event.target.closest('[data-x-filter]');if(f){filter=f.dataset.xFilter;page=0;renderMeme();}
    const p=event.target.closest('[data-x-page]');if(p){page=Math.max(0,page+Number(p.dataset.xPage));renderMeme();}
    const sp=event.target.closest('[data-x-stock-page]');if(sp){stockPage=Math.max(0,stockPage+Number(sp.dataset.xStockPage));renderStocks();}
  });
  document.addEventListener('input',event=>{
    if(event.target.id==='xMemeSearch'){search=event.target.value;page=0;renderMeme();}
    if(event.target.id==='xStockSearch'){stockSearch=event.target.value;stockPage=0;renderStocks();}
  });
  document.addEventListener('submit',event=>{
    if(event.target.id!=='xDetailSearch')return;event.preventDefault();
    const address=new FormData(event.target).get('address');if(/^0x[\da-f]{40}$/i.test(address))location.hash=link(address);
  });
  window.addEventListener('resize',()=>detailChart?.resize());
  window.XLayerUI={render,detail};
})();
