/* Unified product workspace. Rendering and navigation only read our cache. */
(() => {
  const tr=(zh,en)=>typeof LANG!=='undefined'&&LANG==='en'?en:zh;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names={NVDA:'英伟达',AAPL:'苹果',TSLA:'特斯拉',MSFT:'微软',GOOGL:'谷歌',AMZN:'亚马逊',AMD:'超威半导体',INTC:'英特尔',BABA:'阿里巴巴',TSM:'台积电',MU:'美光科技',NFLX:'奈飞',SNDK:'闪迪',QQQ:'纳斯达克100 ETF',SPY:'标普500 ETF'};
  const company=t=>esc(t)+(names[t]?tr(' · '+names[t],''): '');
  const chainNames={'196':'X Layer','56':'BNB Chain','4663':'Robinhood Chain'};
  const sectorNames={'芯片':'Semiconductors','交易所':'Exchanges','支付':'Payments','托管':'Custody','加密国库':'Crypto treasury'};
  const num=v=>v==null?tr('未提供','Unavailable'):Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
  const usd=v=>v==null?tr('未提供','Unavailable'):(()=>{const n=Number(v);const zh=tr('zh','en')==='zh';if(n>=1e9)return zh?'约'+(n/1e9).toFixed(2)+'亿':'~'+(n/1e9).toFixed(2)+'B';if(n>=1e6)return zh?'约'+Math.round(n/1e4)+'万':'~'+(n/1e6).toFixed(n>=1e7?0:1)+'M';if(n>=1e4&&zh)return '约'+Math.round(n/1e4)+'万';return '$'+n.toLocaleString('en-US',{maximumFractionDigits:n>0&&n<0.01?8:2});})();
  const pct=v=>v==null?tr('未提供','Unavailable'):(v>0?'+':'')+Number(v).toFixed(2)+'%';
  const date=t=>t?new Date(t).toLocaleString(tr('zh-CN','en-GB'),{hour12:false}):tr('未采集','Not collected');
  const age=t=>!t?tr('未采集','Not collected'):tr(`${Math.max(0,Math.floor((Date.now()-t)/60000))} 分钟前`,`${Math.max(0,Math.floor((Date.now()-t)/60000))}m ago`);
  const fresh=t=>t&&Date.now()-t<=900000;
  const short=a=>a?a.slice(0,7)+'…'+a.slice(-5):'—';
  const chain=a=>String(a.chainId??a.chain??a.chainIndex??'196');
  const key=a=>chain(a)+':'+String(a.token??a.tokenContractAddress??'').toLowerCase();
  const same=(a,b)=>key(a)===key(b);
  const detailLink=a=>'#detail/'+chain(a)+'/'+a.token;
  const pairLink=r=>'#pair/'+chain(r)+'/'+(r.stock??r.tokenContractAddress)+(r.pool?'?pool='+r.pool:'');
  const explorer=(address,c,type='address')=>(c==='4663'?'https://robinhoodchain.blockscout.com':c==='56'?'https://www.oklink.com/bsc':'https://www.oklink.com/xlayer')+'/'+type+'/'+encodeURIComponent(address);
  const okxLink=a=>'https://web3.okx.com/token/'+({'196':'x-layer','56':'bsc','4663':'robinhood-chain'}[chain(a)])+'/'+encodeURIComponent(a.token);
  const empty=text=>`<div class="x-empty">${text}</div>`;
  const card=(title,value,note,href,attrs='')=>`<div class="kpi" ${attrs}><span class="kpi-label">${title}</span>${href?`<a href="${esc(href)}" class="kpi-value mono">${value}</a>`:`<strong class="kpi-value mono">${value}</strong>`}<span class="kpi-note">${note??''}</span></div>`;
  const docs={
    overview:['总览帮助你从最新线索和已核验关系中选择下一步查看的资产。数字只代表本站已发现的覆盖范围；进入 Meme 雷达查看资产，进入配对分析检查证据。','Overview helps you choose assets from recent leads and verified relationships. Counts describe this product’s discovered coverage. Use Meme Radar for assets and Pair Analysis for evidence.'],
    meme:['比较股票相关 Meme。重点关系要求核验有效且池流动性新鲜、至少 1,000 美元；名称候选只有名称线索。同名合约分别保存，可搜索、筛选、排序并进入详情。','Compare stock-related Memes. Priority requires current verification and fresh pool liquidity of at least $1,000; name leads only have naming evidence. Each contract retains its own record. Search, filter, sort and open details.'],
    stock:['按股票标的查看各网络的代币版本及关联 Meme。代码是上游给出的证券代码，不是排名或价格；已核实的数字代码显示市场和公司名。目录收录不证明链上合约由该公司授权发行。','Browse token versions and related Memes by underlying stock. Codes are provider-supplied security identifiers, not ranks or prices. Identified numeric codes show the market and company name. Catalogue inclusion does not verify that a contract is authorised by the company.'],
    pair:['选择股票代币部署及具体池，核对双方合约、核验区块和流动性，再比较实际采集的价格。未覆盖的池、缺少的价格或历史会显示原因。','Select a stock-token deployment and a pool. Inspect both contracts, verification block and liquidity, then compare observed prices. Missing pool coverage, prices or history are explained.'],
    leads:['展示首次发现或证据状态变化的关系事件。时间是本站发现时间，不一定是链上发生时间。全部历史保存在数据库中，可继续加载更早记录。','Shows relationships first detected or whose evidence changed. Times are product detection times, not necessarily on-chain event times. Saved history is paginated and retained in the database.'],
    ranking:['在当前核验有效的资产中，按上游 24 小时成交额排序。成交额不等于净流入，不代表上涨潜力；缺失值排在后面，历史值另行标记。','Ranks currently verified assets by source-reported 24-hour volume. Volume is not net inflow or expected returns. Missing values sort last and historical values are labelled.'],
    baskets:['用同一行业符合条件的关联资产观察整体变化。至少 3 个成分，基期设为 100，成分和基期权重固定。旧行情不会删除成分；缺少新鲜行情时保留上次指数并标记历史。不同篮子的基期可能不同。','Tracks eligible related assets in a sector. At least three members are required, with a fixed base of 100 and fixed base weights. Stale quotes do not remove members; the last index is marked historical when fresh inputs are missing. Basket base dates may differ.'],
    coverage:['按网络显示已发现候选及可用行情，帮助识别覆盖差距。只汇总新鲜且同口径的链上值；没有采集到的数据不等于没有交易，也不代表全链规模。','Shows discovered candidates and available quotes by network to reveal coverage gaps. Only fresh comparable on-chain values are summed. Uncollected data does not mean no trades or represent the entire chain.'],
    sources:['说明每个提供方实际覆盖目录、行情、成交和关系的哪一部分。每轮结束后等待 5 分钟；大目录分批轮换。请求上限是本站保护配置，达到额度后等待后续采集，不会因浏览页面增加调用。','Shows each provider’s catalogue, quote, trade and relationship capabilities. Collection waits five minutes after each round; large catalogues rotate in batches. Request caps are local safeguards. Browsing does not trigger upstream requests.'],
    quality:['按当前保存数据统计：新鲜指 15 分钟内观察到有效数值，历史指更早或无明确时间的有效数值，缺失表示未提供。0 属于有效数值，与缺失分开。配对证据的近期复核窗口为 1 小时。此面板衡量覆盖，不是安全评分。','Counts saved observations: fresh means a valid value observed within 15 minutes, historical means older or undated, and missing means unavailable. Zero is a value and is counted separately. Recent pair verification uses a one-hour window. This measures coverage, not safety.'],
    roadmap:['仅展示已具备输入的指标。活跃度评分、股票侧流动性净流入等需要额外数据，未具备时不输出估算分数；可先使用实际成交和已确认配对池证据。','Only metrics with sufficient inputs are calculated. Full heat and Stock Flow need additional data and are not replaced with estimated scores. Observed trades and verified pool evidence remain available.'],
    detail:['先看这条资产与股票的关系是否有证据，再检查池规模、数据时间和成交。合约地址区分同名币；名称相似只是候选线索。','Check relationship evidence first, then pool size, observation time and activity. Contract addresses distinguish namesakes; a similar name is only a lead.'],
    prices:['价格图使用本站真实保存的采样点。样本不足时不能判断趋势，空白时段不补画。价格观察不是可成交报价。','Charts use actual saved samples. Insufficient samples cannot establish a trend and gaps are not filled. Observed prices are not executable quotes.'],
    activity:['列出本站已保存的买卖记录，可打开交易哈希核查。采集按预算分批进行，可能有分页缺口；记录条数不能当作全市场 24 小时成交次数。','Lists saved buys and sells with transaction links when available. Collection is budgeted and may have pagination gaps. Saved record counts are not market-wide 24-hour trade counts.'],
    timeline:['按时间追溯该资产的关系发现及证据变化。旧线索持续保留；市场没有变化时不会为了刷新页面制造新事件。','Traces relationship discovery and evidence changes for this asset. Old leads persist; unchanged markets do not generate artificial events.'],
    pools:['查看实际覆盖到的池、双方储备和费用。图中份额以已覆盖且新鲜的池为分母，不代表全链份额；费率不是 LP 收益率。','Inspect covered pools, reserves and fees. Shares use fresh covered pools as their denominator, not the entire chain. Fees are not LP returns.'],
    comparison:['把股票代币与 Meme 的共同采样点归一化到同一起点，比较相对变化。不同价格不能直接相减；共同样本不足时显示等待，相关走势也不证明因果关系。','Normalises shared stock-token and Meme samples to a common starting point. Different prices cannot simply be subtracted. Insufficient shared samples show a waiting state; similar returns do not imply causality.'],
    trades:['24 小时成交总次数来自批量行情接口；买卖拆分仅部分热门榜资产有值。窗口相同但来源覆盖不同，未提供的拆分显示缺失，不能用本站部分成交记录补成全量次数。','24-hour total trades come from batch quotes; buy/sell splits are available for some hot-list assets only. Coverage differs even with the same window. Missing splits are not replaced by partially collected local trades.'],
    price:['上游最近观察到的美元价格；时间是该字段的采集时间，过期值会标记历史。不能视为当前可成交价格。','The latest observed USD price. Each field retains its observation time and stale values are labelled historical. This is not an executable price.'],
    holders:['上游统计的持币地址数量，不等于独立用户数；一个用户可拥有多个地址，池和托管地址也可能计入。','Source-reported holder addresses, not unique users. One user may have several addresses and pools or custodians may be included.'],
    priority:['去重后的关联资产数：关系已在 1 小时内核验，配对池 15 分钟内有至少 1,000 美元流动性。点击数字查看对应列表。','Unique related assets with verification within one hour and pool liquidity of at least $1,000 observed within 15 minutes. Open the count to inspect the list.'],

    evidence:['同链池双方地址与股票目录核对。证明配对，不证明安全或官方合作。','Pool addresses are checked against a stock catalogue on the same chain. Pairing does not certify safety or affiliation.'],
    liquidity:['已覆盖池的总流动性，不是锁定资产。按池地址去重，点击可查看范围与时间。','Liquidity in covered pools, not locked assets. Deduplicated by pool; inspect scope and observation time.'],
    premium:['股票代币价 ÷（每枚对应股数 × 股票参考价）−1；缺少独立价格、换算比例或对齐时间不计算。','Token price / (shares per token × stock reference) − 1. Requires independent prices, a ratio and aligned timestamps.'],
    holder:['前十地址持仓比例之和。地址不等于个人；交易所、池或销毁地址可能在内。','Share held by the ten largest addresses. Addresses are not individuals and may include pools, exchanges or burn addresses.'],
    flow:['已验证池中股票侧 LP 加入减退出；不使用储备余额差替代。事件尚未完整归集。','Stock-side LP deposits minus withdrawals in verified pools. Reserve changes are not a substitute; event coverage is incomplete.'],
    heat:['活跃度评分需要成交量增速、持币地址增速和股票资产规模。输入不足时不生成分数。','Full heat requires volume growth, holder growth and stock-asset scale. Missing inputs do not produce a score.'],
    volume:['滚动24小时美元成交额；场内与 DEX 分开。未知值不视为零，来源与时间逐字段保存。','Rolling 24-hour USD volume; exchange and DEX scopes stay separate. Unknown is not zero; provenance and time are stored per field.'],
    age:['首次发现是本站首次保存的时间，不一定是代币发行时间。','First seen means first saved by this product, not necessarily token creation.'],
  };
  const help=id=>`<button type="button" class="v2-help" data-help="${id}" aria-label="${tr('查看页面说明','Read page guide')}" aria-haspopup="dialog"><span aria-hidden="true">?</span>${tr('页面说明','Page guide')}</button>`;
  const tip=(id,label)=>`${label}<button type="button" class="v2-tip" data-help="${id}" aria-label="${tr('查看说明','Open guide')}: ${esc(tr(...(topicNames[id]??['',''])))}" aria-haspopup="dialog"><span aria-hidden="true">?</span></button>`;
  const aiDone=new Map();
  let lastListHash=location.hash||'#live';
  function trackListHash(){const p=(route().parts[0]??'');if(['live','meme','stock','pair','events'].includes(p))lastListHash=location.hash;}
  function aiRich(text){
    let html=esc(text);
    html=html.replace(/【([A-Za-z0-9.$-]{1,12})】/g,(m,sym)=>`<a href="#meme?q=${encodeURIComponent(sym)}">${sym}</a>`);
    html=html.replace(/^(今日一句话|值得看|警惕|数据边界|Today in one line|Worth a look|Caution|Data bounds)([:：])/gm,'<strong>$1$2</strong>');
    return html;
  }
  function paintAI(el,e){
    const collapsible=el.id==='v2Briefing';
    el.innerHTML=aiRich(e.text);
    el.classList.toggle('v2-ai-fold',collapsible&&el.scrollHeight>170);
    let s=el.nextElementSibling;
    if(!s||!s.classList.contains('v2-ai-at')){s=document.createElement('small');s.className='v2-ai-at';el.after(s);}
    s.textContent=tr('生成于','Generated at')+' '+new Date(e.at).toLocaleTimeString()+' · DeepSeek';
    if(el.classList.contains('v2-ai-fold')){
      const b=document.createElement('button');b.type='button';b.className='v2-ai-more';b.textContent=tr('展开完整简报 ▾','Full briefing ▾');
      b.addEventListener('click',()=>{const open=el.classList.toggle('v2-ai-open');b.textContent=open?tr('收起 ▴','Collapse ▴'):tr('展开完整简报 ▾','Full briefing ▾');});
      s.after(b);
    }
  }
  async function fillAI(){
    const lang=tr('zh','en');
    for(const el of document.querySelectorAll('.v2-ai-text')){
      const key=el.id==='v2Briefing'?'briefing':el.dataset.chain+':'+el.dataset.token;
      const done=aiDone.get(key);
      if(done&&done.lang===lang){paintAI(el,done);continue;}
      el.textContent=tr('正在生成…','Generating…');
      try{
        const r=el.id==='v2Briefing'
          ?await(await fetch('/api/ai/briefing?lang='+lang)).json()
          :await(await fetch('/api/ai/insight/'+el.dataset.chain+'/'+el.dataset.token+'?lang='+lang)).json();
        if(!r.text){el.textContent=tr('AI 解读暂不可用','AI readout unavailable');continue;}
        const entry={lang,text:r.text,at:r.at};
        aiDone.set(key,entry);paintAI(el,entry);
      }catch{el.textContent=tr('AI 解读暂不可用','AI readout unavailable');}
    }
  }
  const guideTopics={
    overview:['overview','priority','evidence','liquidity','leads','ranking','baskets','coverage','quality','sources','roadmap'],
    meme:['meme','evidence','liquidity','volume','trades','age'],
    stock:['stock','premium','volume','sources'],
    pair:['pair','evidence','premium','pools','comparison','holder','activity'],
    detail:['detail','evidence','price','volume','trades','holders','holder','prices','activity','timeline'],
    leads:['leads']
  };
  const topicNames={overview:['总览','Overview'],priority:['重点关系','Priority relationships'],evidence:['关系证据','Relationship evidence'],liquidity:['池流动性','Pool liquidity'],leads:['最新线索与历史','Leads and history'],ranking:['活动排行','Activity ranking'],baskets:['行业篮子','Sector baskets'],coverage:['跨链覆盖','Chain coverage'],quality:['数据可用性','Data availability'],sources:['来源与更新','Sources and updates'],roadmap:['指标进度','Metric availability'],meme:['Meme 雷达','Meme Radar'],volume:['成交额','Volume'],trades:['成交次数','Trade counts'],age:['首次发现','First seen'],stock:['股票雷达','Stock Radar'],premium:['跨市场溢价','Cross-market premium'],pair:['配对分析','Pair Analysis'],pools:['LP 结构','LP structure'],comparison:['对比走势','Comparative returns'],holder:['持仓集中度','Holder concentration'],holders:['持有人','Holders'],activity:['最近成交','Recent trades'],detail:['资产详情','Asset detail'],price:['价格','Price'],prices:['价格历史','Price history'],timeline:['关系时间线','Relationship timeline']};
  let openHelp=null,helpTrigger=null,helpLanguage=null;
  function closeHelp(){const el=document.getElementById('v2HelpDialog');if(el)el.hidden=true;const trigger=helpTrigger?.isConnected?helpTrigger:document.querySelector(`[data-help="${openHelp}"]`);openHelp=null;document.body.classList.remove('v2-help-open');trigger?.focus();}
  function showHelp(id,trigger){
    if(!docs[id])return;openHelp=id;helpLanguage=tr('zh','en');if(trigger)helpTrigger=trigger;
    let el=document.getElementById('v2HelpDialog');if(!el){el=document.createElement('div');el.id='v2HelpDialog';el.className='v2-help-overlay';document.body.append(el);el.addEventListener('click',e=>{if(e.target===el||e.target.closest('[data-help-close]'))closeHelp();});}
    const title=tr(...topicNames[id])+' · '+(guideTopics[id]?tr('页面说明','Page guide'):tr('说明','Guide'));
    const topics=[...new Set(guideTopics[id]??[id])];
    el.innerHTML=`<div class="v2-help-dialog" role="dialog" aria-modal="true" aria-labelledby="v2HelpTitle" aria-describedby="v2HelpContent" tabindex="-1"><div class="panel-head"><h2 id="v2HelpTitle">${esc(title)}</h2><button type="button" data-help-close aria-label="${tr('关闭说明','Close help')}">×</button></div><div id="v2HelpContent">${topics.map(topic=>`<section class="v2-guide-topic"><h3>${esc(tr(...topicNames[topic]))}</h3><p>${esc(tr(...docs[topic]))}</p></section>`).join('')}</div><small>${tr('按 Esc 或点击遮罩关闭','Press Escape or click outside to close')}</small></div>`;
    el.hidden=false;document.body.classList.add('v2-help-open');el.querySelector('[data-help-close]').focus();
  }
  const logo=a=>/^https:\/\//.test(a.logoUrl??'')?`<img class="v2-logo" src="${esc(a.logoUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:`<span class="v2-logo placeholder" aria-hidden="true">${esc((a.symbol??a.tokenSymbol??'?').slice(0,1))}</span>`;
  const field=(a,f,money=false)=>{const at=a.fieldTimes?.[f],provider=a.fieldSources?.[f]??a.provider??'OKX';return `<span title="${esc(provider+' · '+date(at))}" class="${fresh(at)?'':'v2-muted'}">${(money?usd:num)(a[f])}</span>${at&&!fresh(at)?`<small>${tr('历史值','Historical')} · ${age(at)}</small>`:''}`;};
  let snapshot, feed, lastLang, charts=[], sequence=0, eventRows=[], eventCursors={}, eventsLoaded=false,eventBusy=false,eventError=false;
  const cache=new Map(),pending=new Map(),expandedGroups=new Set();
  let activeHash='';
  const route=()=>{const [path,q='']=location.hash.slice(1).split('?');return {parts:(path||'live').split('/'),q:new URLSearchParams(q)};};
  const setQuery=(values)=>{const r=route();for(const[k,v]of Object.entries(values))v==null||v===''?r.q.delete(k):r.q.set(k,v);const hash='#'+r.parts.join('/')+(r.q.size?'?'+r.q:'');window.history.replaceState(null,'',location.pathname+location.search+hash);render(snapshot);};
  const relations=a=>(feed.relations??[]).filter(r=>r.token===a.token&&chain(r)===chain(a));
  const verified=a=>relations(a).filter(r=>r.status==='verified'&&Date.now()-r.checkedAt<=3600000);
  const status=r=>r.status==='verified'?tr('地址已核验','Address verified'):r.status==='invalid'?tr('证据已变化','Evidence changed'):tr('待核验','Pending verification');
  function summarySources(){
    const src=feed.sources??[];
    const labels={ready:tr('已更新','updated'),partial:tr('部分覆盖','partial coverage'),starting:tr('初始化','starting'),pending:tr('待采集','pending'),stale:tr('保留历史','historical'),error:tr('连接失败','failed'),unconfigured:tr('需要 API Key','API key required')};
    document.getElementById('sourceStatus').textContent=tr('已接入数据源：','Data sources: ')+src.map(s=>`${s.provider} (${labels[s.status]??esc(s.status)})`).join(' · ')+tr(' · 每 5 分钟采集，字段时效独立',' · 5-minute collection; field timestamps differ');
  }
  function eventHtml(e){const c=chain(e),a=(feed.assets??[]).find(a=>a.token===e.asset&&chain(a)===c);const sym=e.symbol??a?.symbol??short(e.asset);const tag=e.kind==='verified'?`<em class="tag on">${tr('配对确认','Pair confirmed')}</em>`:e.kind==='invalid'?`<em class="tag">${tr('证据变化','Evidence changed')}</em>`:`<em class="tag">${tr('更新','Update')}</em>`;return `<a class="x-signal v2-event" href="#meme?q=${encodeURIComponent(sym)}"><span class="v2-pair-line"><strong>${esc(sym)}</strong> <i aria-hidden="true">↔</i> <b>${esc(e.ticker??'—')}</b></span>${tag}<small>${chainNames[c]??c} · ${date(e.t)} · ${tr('本站发现时间','Detected')}</small></a>`;}
  function overview(){
    const m=feed.metrics??{},cov=m.liquidityCoverage??{};
    const related=(feed.assets??[]).filter(a=>a.kind==='candidate'&&verified(a).length).sort((a,b)=>(b.volume24h??-1)-(a.volume24h??-1));
    return `<div class="hero x-hero"><div class="v2-hero-copy"><span class="eyebrow">STOCKSMEME / RELATIONSHIP INTELLIGENCE</span><h2>${tr('发现股票相关 Meme，查看链上配对证据。','Find stock-related memes with on-chain pair evidence.')} ${help('overview')}</h2></div><form id="v2Search"><input name="q" placeholder="${tr('搜索股票、币名或合约地址','Search stocks, tokens or contract addresses')}" aria-label="${tr('全局搜索','Global search')}" required><button>${tr('搜索','Search')}</button></form></div>
      <section class="panel x-ai"><h2>${tr('今日变化摘要','Today changes')}</h2><p class="hint">${tr('由 DeepSeek 基于本站已采集字段生成，只解读已有数据，不预测价格。','Generated by DeepSeek from collected fields only; no price forecasts.')}</p><div class="v2-ai-text" id="v2Briefing"></div></section><div class="kpis">${card(tip('priority',tr('达到流动性门槛的 Meme','Memes above the liquidity floor')),num(m.actionableAssets),tr('已核验且新鲜池流动性 ≥ $1,000','Verified with fresh pool liquidity ≥ $1,000'),'#meme?filter=related','data-kpi-key="actionableAssets"')}${card(tip('evidence',tr('已确认的股票配对池','Confirmed stock pair pools')),num(m.verifiedPools),tr('按链与池地址去重','Deduplicated by chain and pool'),'#pair','data-kpi-key="verifiedPools"')}${card(tip('liquidity',tr('已收录股票配对池流动性','Listed pair-pool liquidity')),usd(m.pairedLiquidityUsd),`${num(cov.valued)} / ${num(cov.total)} ${tr('池有新鲜估值','pools with fresh valuations')} · ${tr('覆盖数量变化不等于资金进出','coverage changes are not flows')}`,'','data-kpi-key="pairedLiquidityUsd"')}${card(tip('leads',tr('24h 新增配对','New pairs in 24h')),num(m.newRelations24h),tr('按首次发现时间统计','Counted by first detection'),'#events','data-kpi-key="newRelations24h"')}</div>
      <div class="x-grid v2-overview-grid"><section class="panel"><div class="panel-head"><h2>${tr('最新发现与变化','Latest discoveries and changes')}${tip('leads','')}</h2><a href="#events">${tr('全部历史','Full history')} →</a></div>${(feed.signals??[]).slice(0,6).map(eventHtml).join('')||empty(tr('尚无关系变化，采集继续运行。','No relationship changes yet; collection continues.'))}</section><section class="panel"><div class="panel-head"><h2>${tr('关联 Meme 成交榜','Related-meme volume')}${tip('priority','')}</h2><a href="#meme?filter=related">${tr('全部','All')} →</a></div>${related.slice(0,6).map(a=>`<a class="x-signal" href="#meme?q=${encodeURIComponent(a.symbol)}"><span class="v2-asset-identity">${logo(a)}<strong>${esc(a.symbol)}</strong></span><span>${verified(a).map(r=>esc(r.ticker)).join(' / ')}</span><span>${field(a,'volume24h',true)}<small>24h · ${chainNames[chain(a)]}</small></span></a>`).join('')||empty(tr('等待可核查的配对证据','Waiting for verifiable pair evidence'))}</section></div>
      <section class="panel"><h2>${tr('行业主题指数','Sector theme indices')}${tip('baskets','')}</h2><p class="hint">${tr('固定基期 100、固定成分与市值权重。行情过期不删除成分；缺口不补画。','Fixed base 100, constituents and market-cap weights. Stale prices do not remove members; gaps are not backfilled.')}</p><div class="v2-baskets">${(feed.sectors??[]).filter(s=>s.lastValue!=null||s.value!=null).map((s,i)=>`<details class="x-basket" data-open-id="basket-${s.chainId}-${s.sector}"><summary><strong>${tr(s.sector,sectorNames[s.sector]??s.sector)}</strong> <span>${s.value!=null?num(s.value):s.lastValue!=null?num(s.lastValue)+' · '+tr('历史','Historical'):tr('收集成分中','Collecting members')}</span><small>${chainNames[s.chainId]??'X Layer'} · ${s.members}/3 ${tr('成分门槛','minimum members')}</small></summary><p>${s.value!=null?tr('当前指数','Current index'):tr('等待合格成分或新鲜行情','Waiting for eligible members or fresh prices')} · ${date(s.lastAt??s.baseAt)}</p>${s.history?.length>=2?`<div class="chart v2-mini" data-chart="basket-${i}"></div>`:''}${(s.components??[]).map(c=>`<p><a href="#detail/${s.chainId??'196'}/${c.token}">${esc(c.symbol)}</a> ${c.weight!=null?pct(c.weight*100):''}</p>`).join('')}</details>`).join('')||((feed.sectors??[]).length?`<p class="hint">${tr('另有','')}${(feed.sectors??[]).filter(s=>s.lastValue==null&&s.value==null).length} ${tr('个行业主题筹备中，达到成分门槛后发布，不虚构指数。','sector themes in preparation; published only when eligible, no synthetic index.')}</p>`:empty(tr('暂无行业篮子数据','No basket data yet')))}</div></section>
      <section class="panel"><h2>${tr('各链数据概况','Chain overview')}${tip('coverage','')}</h2><p>${tr('仅汇总新鲜且同口径的链上数据；场内成交不加入。','Only fresh, comparable on-chain data is aggregated; exchange volume is excluded.')}</p>${(feed.distribution??[]).map(d=>`<a class="v2-distribution" href="#meme?filter=all&chain=${d.chainId}"><strong>${esc(d.name)}</strong><span>${num(d.assets)} ${tr('已发现候选','discovered candidates')}</span><span>24h ${usd(d.volume.value)} <small>${d.volume.known}/${d.assets} ${tr('有值','available')}</small></span><span>${tr('池流动性','Pool liquidity')} ${usd(d.liquidity.value)}</span></a>`).join('')}</section>
      <p class="v2-sources-line">${tr('数据来源：OKX RWA API · 三链 RPC 直读（BNB / X Layer / Robinhood Chain）· GeckoTerminal / DexScreener 行情 · Binance / Robinhood 公开接口。每个数字标注采集时间，缺失如实标注，不用旧数据冒充实时。','Sources: OKX RWA API · direct RPC on three chains (BNB / X Layer / Robinhood Chain) · GeckoTerminal / DexScreener quotes · Binance / Robinhood public APIs. Every number carries its observation time; missing stays missing.')}</p>`;
  }
  function memePage(){
    const q=route().q,filter=q.get('filter')??'related',search=(q.get('q')??'').toLowerCase(),sort=q.get('sort')??'volume24h';
    let groups=(feed.groups??[]).map(g=>({...g,members:g.members.filter(a=>{
      const rs=verified(a);if(q.get('chain')&&chain(a)!==q.get('chain'))return false;
      if(search&&![a.symbol,a.name,a.token,...relations(a).map(r=>r.ticker)].join(' ').toLowerCase().includes(search))return false;
      if(Number(q.get('minLiquidity')??0)>(a.liquidity??-1))return false;
      if(filter==='related')return rs.some(r=>(r.liquidityUsd??0)>=1000&&fresh(r.liquidityAt??r.checkedAt));
      if(filter==='verified')return rs.length>0;if(filter==='name')return !!a.match&&!rs.length;if(filter==='history')return !fresh(a.fieldTimes?.price??a.updatedAt);return true;
    })})).filter(g=>g.members.length);
    const poolActivity=a=>Math.max(0,...relations(a).filter(r=>r.status==='verified').map(r=>Number(r.poolMarket?.volume24h??r.liquidityUsd??0)||0));
    const relSort=(filter==='related'||filter==='verified')&&sort==='volume24h';
    const compare=(a,b)=>{const av=relSort?poolActivity(a):a[sort],bv=relSort?poolActivity(b):b[sort];return (bv==null?-Infinity:Number(bv))-(av==null?-Infinity:Number(av))||key(a).localeCompare(key(b));};
    groups.forEach(g=>g.members.sort(compare));groups.sort((a,b)=>compare(a.members[0],b.members[0]));
    const pages=Math.max(1,Math.ceil(groups.length/20)),page=Math.min(Math.max(0,Number(q.get('page'))||0),pages-1);
    const options=[['related',tr('配对池≥$1,000','Pools at least $1,000')],['verified',tr('已确认配对','Confirmed pairs')],['name',tr('仅名称匹配','Name match only')],['all',tr('全部资产','All assets')],['history',tr('行情较旧','Stale quotes')]];
    const row=(a,extra='',child=false)=>`<tr data-asset="${esc(a.token)}" class="${child?'v2-member-row':''}"><td>${logo(a)}<a href="${detailLink(a)}"><strong>${esc(a.symbol)}</strong></a><small>${chainNames[chain(a)]} · ${short(a.token)} · ${esc(a.provider??'OKX')}</small>${extra}</td><td>${(()=>{const rr=relations(a);const shown=rr.slice(0,2).map(r=>`<a href="${pairLink(r)}">${esc(r.ticker)} · ${status(r)}</a>`).join('<br>');const more=rr.length>2?`<br><small title="${esc(rr.map(r=>r.ticker).join(', '))}">+${rr.length-2}</small>`:'';return shown+more||tr('待核实','Pending');})()}</td><td data-field="liquidity">${field(a,'liquidity',true)}</td><td data-field="volume24h">${field(a,'volume24h',true)}</td><td>${field(a,'txs24h')}<small>${tr('买 / 卖','Buys / sells')}: ${field(a,'buys24h')} / ${field(a,'sells24h')}</small></td><td data-field="price">${field(a,'price',true)}</td><td>${field(a,'holders')}</td><td>${age(a.fieldTimes?.price??a.updatedAt)}<small>${tr('本站首次收录','First listed')} ${date(a.firstSeen)}</small></td></tr>`;
    const head=`<thead><tr>${[tr('资产','Asset'),tr('关联股票','Related stock'),tr('资产总流动性','Asset liquidity'),tr('资产 24h 成交额','Asset 24h volume'),tr('24h 成交次数','24h trades'),tr('Meme 价格','Meme price'),tr('持币地址数','Holder addresses'),tr('行情更新时间','Quote time')].map(h=>`<th>${h}</th>`).join('')}</tr></thead>`;
    return `<section class="panel"><div class="tab-group">${options.map(([id,t])=>`<button data-v2-filter="${id}" class="tab ${id===filter?'active':''}">${t}</button>`).join('')}${tip('meme','')}</div><div class="x-toolbar"><input id="xMemeSearch" value="${esc(q.get('q')??'')}" placeholder="${tr('名称、股票或 CA','Name, stock or CA')}" aria-label="${tr('搜索资产','Search assets')}"><select id="v2Sort" aria-label="${tr('排序','Sort')}">${[['volume24h',tr('24h 成交额','24h volume')],['liquidity',tr('资产总流动性','Asset liquidity')],['marketCap',tr('市值','Market cap')],['holders',tr('持币地址数','Holder addresses')],['change24h',tr('24h 涨跌','24h change')],['firstSeen',tr('本站首次收录','First listed')]].map(([id,t])=>`<option value="${id}" ${sort===id?'selected':''}>${t}</option>`).join('')}</select><details class="v2-filters" data-open-id="advanced-filters" ${q.get('chain')||q.get('minLiquidity')?'open':''}><summary>${tr('更多筛选','More filters')}${q.get('chain')||q.get('minLiquidity')?' · '+tr('已启用','Active'):''}</summary><div class="v2-filter-fields"><select id="v2Chain" aria-label="${tr('网络筛选','Network filter')}"><option value="">${tr('全部网络','All networks')}</option>${Object.entries(chainNames).map(([id,n])=>`<option value="${id}" ${q.get('chain')===id?'selected':''}>${n}</option>`).join('')}</select><input id="v2Min" type="number" min="0" value="${esc(q.get('minLiquidity')??'')}" placeholder="${tr('最低流动性 $','Minimum liquidity $')}" aria-label="${tr('最低流动性','Minimum liquidity')}"></div></details></div><p class="hint">${tr('同名合约可展开查看；缺失与历史值分别标记。','Expand namesakes to inspect each contract; missing and historical values are labelled.')}</p><div id="xMemeRows">${groups.length?`<div class="scroll v2-table-scroll"><table class="tbl v2-meme-table">${head}<tbody>${groups.slice(page*20,page*20+20).map(g=>{
      const groupId=g.symbol.normalize('NFKC').trim().toLowerCase(),expanded=expandedGroups.has(groupId);
      const toggle=g.members.length>1?`<button type="button" class="v2-group-toggle" data-v2-group="${esc(groupId)}" aria-expanded="${expanded}">${expanded?'−':'+'} ${g.members.length-1} ${tr('个同名合约','more contracts')}</button>`:'';
      return row(g.members[0],toggle)+(expanded?g.members.slice(1).map(a=>row(a,'',true)).join(''):'');
    }).join('')}</tbody></table></div>`:empty(tr('当前筛选无记录，历史仍保留。','No matches; historical records remain saved.'))}</div><div class="x-pager"><button data-v2-page="${page-1}" ${!page?'disabled':''}>${tr('上一页','Previous')}</button><span>${page+1}/${pages} · ${groups.length} ${tr('组','groups')}</span><button data-v2-page="${page+1}" ${page>=pages-1?'disabled':''}>${tr('下一页','Next')}</button></div></section>`;
  }
  function stockPage(){
    const q=route().q,search=(q.get('q')??'').toLowerCase(),groups=new Map();
    for(const s of feed.stockTokens??[]){if(search&&![s.stockCode,s.tokenSymbol,s.tokenName,s.tokenContractAddress,names[s.stockCode],s.stockIdentity?.nameZh,s.stockIdentity?.nameEn,s.stockIdentity?.code].join(' ').toLowerCase().includes(search))continue;const id=s.stockIdentity?.id||s.stockCode||s.assetId;groups.set(id,[...(groups.get(id)??[]),s]);}
    const relatedCount=stocks=>(feed.relations??[]).filter(r=>r.status==='verified'&&stocks.some(s=>s.tokenContractAddress?.toLowerCase()===r.stock&&chain(s)===chain(r))).length;
    const list=[...groups].sort((a,b)=>relatedCount(b[1])-relatedCount(a[1])||a[0].localeCompare(b[0])),page=Math.max(0,Math.min(Number(q.get('page'))||0,Math.ceil(list.length/20)-1));
    const rows=list.slice(page*20,page*20+20).map(([groupId,stocks])=>{
      const first=stocks[0],ticker=first.stockCode||first.tokenSymbol,identity=first.stockIdentity;
      const title=identity?.status==='identified'?`${esc(tr(identity.nameZh,identity.nameEn))} <small>HKEX · ${esc(identity.code)}</small>`:/^\d+$/.test(ticker)?`${esc(first.tokenName||first.tokenSymbol)} · ${tr('名称待核实','Name unresolved')} <small>${tr('原始代码','Source code')} ${esc(ticker)}</small>`:company(ticker);
      const rs=(feed.relations??[]).filter(r=>stocks.some(s=>s.tokenContractAddress?.toLowerCase()===r.stock&&chain(s)===chain(r))),count=new Set(rs.filter(r=>r.status==='verified').map(r=>key(r))).size;
      return `<details class="x-stock" data-open-id="stock-${esc(groupId)}" ${search&&list.length===1||search===ticker.toLowerCase()?'open':''}><summary><strong>${title}</strong><span>${stocks.length} ${tr('代币版本','token versions')}</span><span>${count} ${tr('已确认关联 Meme','confirmed related memes')}</span></summary><h3>${tr('关联 Meme','Related memes')}</h3>${rs.map(r=>`<a class="x-signal" href="${pairLink(r)}"><strong>${esc((feed.assets??[]).find(a=>a.token===r.token&&chain(a)===chain(r))?.symbol??short(r.token))}</strong><span>${chainNames[chain(r)]} · ${status(r)}</span><span>${usd(r.liquidityUsd)}</span></a>`).join('')||empty(tr('尚未发现已核验配对；目录接入不代表配对已覆盖。','No verified pairs discovered yet; catalogue availability does not imply pair coverage.'))}<details class="v2-versions"><summary>${tr('代币版本','Token versions')}</summary><div class="scroll"><table class="tbl"><thead><tr>${[tr('股票代币 / 网络','Stock token / network'),tr('发行方','Issuer'),tr('代币价格','Token price'),tr('参考股价','Stock reference'),tr('链上价 vs 股价','on-chain vs stock'),tr('24h 成交额 / 市场','24h volume / market'),tr('操作','Actions')].map(t=>`<th>${t}</th>`).join('')}</tr></thead><tbody>${stocks.map(s=>`<tr data-asset="${esc(s.assetId||s.tokenContractAddress||s.instrumentId)}"><td>${esc(s.tokenSymbol)}<small>${tr('来源代码·待确认','Source code·unverified')}: ${esc(s.stockCode)}</small><small>${chainNames[chain(s)]??tr('场内品种','Exchange instrument')} · ${s.tokenContractAddress?`<a href="${pairLink(s)}">${short(s.tokenContractAddress)}</a>`:esc(s.instrumentId)}</small></td><td>${esc(s.issuer)}<small>${tr('来源','Source')} ${(s.providers??[s.provider]).map(esc).join(' / ')}</small></td><td data-field="price">${usd(s.price)}<small>${age(s.updatedAt)} · ${s.priceScope==='exchange'?tr('场内','Exchange'):s.priceScope==='issuer-derived'?tr('发行方参考','Issuer reference'):'DEX'}</small></td><td data-field="stockPrice">${usd(s.stockPrice)}<small>${esc(s.referenceProvider??'')} · ${age(s.referenceAt)}</small></td><td data-field="premium">${pct(s.premium?.value)}<small>${s.premium?.reason?tr('价格或换算条件未满足','Price or conversion conditions unmet'):''}</small></td><td data-field="volume24h">${usd(s.volume24h)}<small>${age(s.fieldTimes?.volume24h??s.updatedAt)} · ${s.volumeScope==='exchange'?tr('场内','Exchange'):'DEX'}</small></td><td>${s.tokenContractAddress?`<a href="${pairLink(s)}">${tr('交易池分析','Pool analysis')} →</a> <button data-copy="${esc(s.tokenContractAddress)}">${tr('复制合约地址','Copy contract')}</button>`:tr('目录记录','Catalogue record')}</td></tr>`).join('')}</tbody></table></div>${identity?.source?`<p><a href="${esc(identity.source)}" target="_blank" rel="noopener">${tr('股票代码核对依据','Stock-code reference')} ↗</a> · ${tr('仅核对标的名称，不证明代币发行授权。','Identifies the underlying name, not token issuance authorisation.')}</p>`:''}</details></details>`;
    }).join('');
    return `<section class="panel"><div class="v2-stock-toolbar"><input id="xStockSearch" value="${esc(q.get('q')??'')}" placeholder="${tr('搜索股票代码、中文名、CA','Search ticker, company or CA')}" aria-label="${tr('搜索股票','Search stocks')}"><span class="hint">${search?`<strong>${tr('搜索','Search')}“${esc(q.get('q')??'')}” · ${list.length} ${tr('个结果','results')}</strong> <button type="button" id="xStockClear" class="v2-clear-search">${tr('清除搜索 ✕','Clear ✕')}</button>`:`${list.length} ${tr('个标的 · 按关联数量排序','underlyings · most relationships first')}`}${tip('stock','')}</div><div id="xStockRows">${rows||empty(tr('暂无匹配股票','No matching stocks'))}</div><div class="x-pager"><button data-v2-page="${page-1}" ${!page?'disabled':''}>${tr('上一页','Previous')}</button><span>${page+1}/${Math.max(1,Math.ceil(list.length/20))} · ${list.length}</span><button data-v2-page="${page+1}" ${(page+1)*20>=list.length?'disabled':''}>${tr('下一页','Next')}</button></div></section>`;
  }
  async function cachedDetail(c,address){const id=c+':'+address;if(cache.has(id)&&Date.now()-cache.get(id).at<20000)return cache.get(id).data;if(pending.has(id))return pending.get(id);const task=fetch('/api/token/'+encodeURIComponent(c)+'/'+encodeURIComponent(address)).then(async r=>{if(!r.ok)throw new Error('unavailable');let d=await r.json();if(!d.asset&&d.token)d={asset:{token:d.token,chainId:c,symbol:d.symbol,name:d.name,firstSeen:d.ts?d.ts*1000:null,updatedAt:d.lastPriceAt,price:d.lastPriceUsd,volume24h:null,holders:null,provider:'BSC RPC',kind:'candidate'},relations:[],samples:[],trades:[],events:[]};cache.set(id,{data:d,at:Date.now()});return d;}).finally(()=>pending.delete(id));pending.set(id,task);return task;}
  let registryState=null,registryAt=0;
async function loadRegistry(){if(registryState&&Date.now()-registryAt<60000)return;try{const r=await(await fetch('/api/registry')).json();registryState=r;registryAt=Date.now();const f=document.querySelector('footer');if(f&&r&&r.configured&&r.contract){let b=f.querySelector('#v2RegistryFooter');if(!b){b=document.createElement('span');b.id='v2RegistryFooter';f.append(' · ',b);}b.innerHTML=`<a href="${r.explorer}" target="_blank" rel="noopener">PairRegistry (X Layer) ${r.contract.slice(0,8)}…${r.contract.slice(-4)} ↗</a> · ${Number(r.onchainCount)} pairs`;}}catch(e){}}
function registryBadge(r){const s=registryState;if(!s||!s.configured||!s.contract)return '';const hit=(s.pairs||[]).find(p=>p.meme===String(r.token||'').toLowerCase()&&p.stock===String(r.stock||'').toLowerCase());if(!hit)return '';const short=s.contract.slice(0,10)+'…'+s.contract.slice(-6);return `<p>⛓ ${tr('链上登记','On-chain registry')} ✓ · <a href="${s.explorer}" target="_blank" rel="noopener">${short} ↗</a> · ${date(hit.registeredAt*1000)}</p>`;}
function proof(r,hideLink=false){return `<article class="x-proof"><div class="panel-head"><h3>${esc(r.ticker)} · ${status(r)}</h3>${hideLink?'':`<a href="${pairLink(r)}">${tr('交易池分析','Pool analysis')} →</a>`}</div><p>${chainNames[chain(r)]} · ${esc(r.protocol)} · ${tr('核验区块','Verified block')} ${num(r.block)} · ${date(r.checkedAt)}</p><p><a href="${explorer(r.pool,chain(r))}" target="_blank" rel="noopener">${tr('池地址','Pool')} ${esc(r.pool)} ↗</a></p><p>${tr('池流动性','Pool liquidity')} ${usd(r.liquidityUsd)} · ${esc(r.liquidityProvider??'OKX')} · ${age(r.liquidityAt)}</p>${registryBadge(r)}<details><summary>${tr('查看交易池双方合约','View both pool contracts')}</summary><p>token0: <code>${esc(r.token0)}</code></p><p>token1: <code>${esc(r.token1)}</code></p><p>${r.wrapper?tr('已核验包装映射','Verified wrapper mapping'):tr('直接配对','Direct pair')}: <code>${esc(r.stockSide)}</code> → <code>${esc(r.stock)}</code></p></details></article>`;}
  let detailChart=null,detailChartEl=null,candleSeries=null,volumeSeries=null,lineSeries=null,priceLine=null,candleDataLen=0;
  let chartMode='candle',chartBar='5m',lastPaintKey='',lastDetail=null,lastTradeIds=new Set(),lastPriceText='';
  const candleMem=new Map();
  const tradeRow=(t,a)=>`<tr><td>${date(t.t)}</td><td>${t.type==='buy'?tr('买入','Buy'):tr('卖出','Sell')}</td><td>${usd(t.volume)}</td><td>${esc(t.dex)}</td><td>${t.hash?`<a href="${explorer(t.hash,chain(a),'tx')}" target="_blank" rel="noopener">${short(t.hash)} ↗</a>`:tr('未提供哈希','Hash unavailable')}</td></tr>`;
  const tradeStatText=a=>a.tradeAt?tr('已保存范围内的成交，可能存在分页缺口，不代表完整历史。','Trades in the saved coverage may have pagination gaps; this is not complete history.'):tr('成交采集排队中，缺失不表示零成交。','Trade collection is queued; missing data does not mean zero trades.');
  async function loadCandles(cid,address,bar,limit=180){
    const key=address+':'+bar,now=Date.now(),hit=candleMem.get(key);
    if(hit&&now-hit.at<20_000)return hit.rows;
    try{
      const r=await(await fetch('/api/candles/'+encodeURIComponent(cid)+'/'+encodeURIComponent(address)+'?bar='+bar+'&limit='+limit)).json();
      if(!r||!Array.isArray(r.rows))return hit?.rows??null;
      candleMem.set(key,{rows:r.rows,at:now});return r.rows;
    }catch(e){return hit?.rows??null;}
  }
  const LW=()=>window.LightweightCharts;
  function killDetailChart(){if(detailChart){try{detailChart.remove();}catch(e){}detailChart=null;}detailChartEl=null;candleSeries=volumeSeries=lineSeries=priceLine=null;candleDataLen=0;}
  function ensureDetailChart(el){
    if(!LW())return null;
    if(!detailChart){
      detailChartEl=el;
      detailChart=LightweightCharts.createChart(el,{
        width:el.clientWidth||600,height:el.clientHeight||380,
        layout:{background:{type:LightweightCharts.ColorType.Solid,color:'transparent'},textColor:'#91a0b5',fontSize:11},
        grid:{vertLines:{color:'rgba(37,48,68,.45)'},horzLines:{color:'rgba(37,48,68,.45)'}},
        rightPriceScale:{borderColor:'rgba(37,48,68,.7)'},
        timeScale:{borderColor:'rgba(37,48,68,.7)',timeVisible:true,secondsVisible:false,rightOffset:3},
        crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
      });
    }
    return detailChart;
  }
  const decOf=v=>v>=100?2:v>=1?4:v>=0.01?6:8;
  const toPts=data=>{let last=0;return data.map(([t,p])=>{const s=Math.max(Math.floor(t/1000),last+1);last=s;return {time:s,value:p};});};
  function syncChartTabs(){
    document.querySelectorAll('#v2ChartTabs [data-chart-mode]').forEach(b=>b.classList.toggle('active',b.dataset.chartMode===chartMode));
    const grp=document.getElementById('v2ChartTabs');if(!grp)return;
    const have=[...grp.querySelectorAll('[data-chart-bar]')];
    if(chartMode==='candle'&&!have.length){['1m','5m','15m','1H'].forEach(b=>{const btn=document.createElement('button');btn.className='tab';btn.dataset.chartBar=b;btn.textContent=b;grp.append(btn);});}
    if(chartMode!=='candle'&&have.length)have.forEach(b=>b.remove());
    grp.querySelectorAll('[data-chart-bar]').forEach(b=>b.classList.toggle('active',chartMode==='candle'&&b.dataset.chartBar===chartBar));
  }
  async function paintDetailChart(d,reset=false){
    const a=d.asset??{},cid=chain(a),el=document.getElementById('v2Price');
    if(!el)return;
    const paintKey=chartMode+':'+chartBar;
    if(paintKey!==lastPaintKey){reset=true;lastPaintKey=paintKey;}
    syncChartTabs();
    const hint=document.getElementById('v2ChartHint');
    if(!LW()){if(hint)hint.textContent=tr('图表组件加载中…','Chart component loading…');return;}
    if(chartMode==='candle'){
      let rows=await loadCandles(cid,a.token,chartBar);
      if(rows&&rows.length){
        if(a.price!=null){rows=[...rows];const i=rows.length-1;rows[i]={...rows[i],c:a.price,h:Math.max(rows[i].h,a.price),l:Math.min(rows[i].l,a.price)};}
        if(hint)hint.textContent=(chartBar==='1m'?tr('1 分钟','1m'):chartBar==='1H'?tr('1 小时','1H'):chartBar+' ')+tr('K 线 · OKX DEX 真实成交 · 虚线为最新价','candles · real OKX DEX trades · dashed line marks the latest price');
        const bars=rows.map(r=>({time:Math.floor(r.t/1000),open:r.o,high:r.h,low:r.l,close:r.c}));
        if(!reset&&candleSeries&&volumeSeries&&candleDataLen===bars.length){
          candleSeries.update(bars[bars.length-1]);
          const vrow=rows[rows.length-1];
          volumeSeries.update({time:Math.floor(vrow.t/1000),value:vrow.vu??vrow.v});
          if(priceLine&&a.price!=null)priceLine.applyOptions({price:a.price});
          return;
        }
        killDetailChart();
        const ch=ensureDetailChart(el);if(!ch)return;
        const last=bars[bars.length-1].close,dec=decOf(last),mm=10**(-dec);
        candleSeries=ch.addCandlestickSeries({upColor:'#2dd4a7',downColor:'#ff5d5d',borderUpColor:'#2dd4a7',borderDownColor:'#ff5d5d',wickUpColor:'#2dd4a7',wickDownColor:'#ff5d5d',priceFormat:{type:'price',precision:dec,minMove:mm}});
        candleSeries.setData(bars);
        volumeSeries=ch.addHistogramSeries({priceScaleId:'',priceFormat:{type:'volume'}});
        volumeSeries.priceScale().applyOptions({scaleMargins:{top:0.82,bottom:0}});
        volumeSeries.setData(rows.map(r=>({time:Math.floor(r.t/1000),value:r.vu??r.v,color:'rgba(96,125,159,.5)'})));
        priceLine=candleSeries.createPriceLine({price:a.price??last,color:'#b9fa6a',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:''});
        candleDataLen=bars.length;
        return;
      }
      if((d.samples??[]).length>=2&&hint)hint.textContent=tr('暂无 K 线返回，显示 5 分钟采样线。','No candles returned yet; showing the 5-minute sample line.');
      else if(hint){hint.textContent=tr('K 线与采样点暂缺，成交后自动出现。','Candles and samples are pending; they appear after trades.');killDetailChart();return;}
    }else if(hint)hint.textContent=tr('分时 · 5 分钟采样 + 最新价','Intraday · 5-minute samples plus the live price');
    const data=(d.samples??[]).map(p=>[p.t,p.price]);
    if(a.price!=null)data.push([Date.now(),a.price]);
    if(data.length<2){killDetailChart();return;}
    const pts=toPts(data);
    if(!reset&&lineSeries){lineSeries.setData(pts);if(priceLine)priceLine.applyOptions({price:pts[pts.length-1].value});return;}
    killDetailChart();
    const ch=ensureDetailChart(el);if(!ch)return;
    lineSeries=ch.addAreaSeries({lineColor:'#b9fa6a',topColor:'rgba(185,250,106,.16)',bottomColor:'rgba(185,250,106,.02)',lineWidth:2});
    lineSeries.setData(pts);
    priceLine=lineSeries.createPriceLine({price:pts[pts.length-1].value,color:'#b9fa6a',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:''});
  }
  function updateDetailIncremental(d){
    const a=d.asset??{};
    void paintDetailChart(d);
    const tbody=document.getElementById('v2TradeBody');
    if(tbody){
      const fresh=(d.trades??[]).filter(t=>!lastTradeIds.has(t.id));
      if(fresh.length){
        tbody.insertAdjacentHTML('afterbegin',fresh.map(t=>`<tr class="trade-new">${tradeRow(t,a).slice(3)}`).join(''));
        while(tbody.children.length>150)tbody.lastElementChild.remove();
      }
    }
    (d.trades??[]).forEach(t=>lastTradeIds.add(t.id));
    const stat=document.getElementById('v2TradeStat');
    if(stat&&a.tradeAt)stat.textContent=tradeStatText(a);
    const priceEl=document.querySelector('#memeDetail [data-kpi="price"] .kpi-value');
    if(priceEl&&a.price!=null){
      const text=usd(a.price);
      if(text!==lastPriceText){priceEl.textContent=text;priceEl.classList.remove('kpi-flash');void priceEl.offsetWidth;priceEl.classList.add('kpi-flash');}
      lastPriceText=text;
    }
  }
  function detailPage(d){
    const a=d.asset??{},rs=d.relations??[];
    return `<div class="page-heading"><a href="${lastListHash}">← ${tr('返回列表','Back to list')}</a><h2>${logo(a)} ${esc(a.symbol)} <span class="x-badge">${chainNames[chain(a)]}</span> ${help('detail')}</h2><p>${esc(a.name)} · ${tr('本站首次收录','First listed')} ${date(a.firstSeen)}</p></div><section class="panel"><div class="address-row"><code>${esc(a.token)}</code><button data-copy="${esc(a.token)}">${tr('复制合约地址','Copy contract')}</button><a href="${explorer(a.token,chain(a))}" target="_blank" rel="noopener">${tr('区块浏览器','Block explorer')} ↗</a><a href="${okxLink(a)}" target="_blank" rel="noopener">${tr('在 OKX 查看资产','View asset on OKX')} ↗</a></div><p>${tr('来源','Source')} ${esc(a.provider??'OKX')} · ${age(a.updatedAt)}${a.error?' · '+tr('部分采集失败，历史保留','Partial collection failure; history retained'):''}</p></section><section class="panel x-verdict"><h2>${rs.some(r=>r.status==='verified')?tr('已确认的股票配对','Confirmed stock pair'):a.kind==='stock'?tr('已收录股票代币','Stock token catalogued'):(rs.length||a.match)?tr('关系待核实','Relationship pending verification'):tr('与股票无关 · 全市场发现候选','Unrelated to stocks · market-wide discovery')}</h2><p>${tr('依据同链双方合约与股票目录核验。配对不代表官方关系或安全认证。','Verified against both contracts and a same-chain stock catalogue. Pairing does not certify affiliation or safety.')}</p>${rs.map(proof).join('')||empty(a.match?tr('名称线索已保存，尚无资金关系证据。','Name lead saved; no capital relationship has been verified.'):tr('该资产来自全市场热门扫描，目前没有与任何股票的名称匹配或配对池记录。','Discovered by market-wide hot scanning; no stock name match or pair-pool record exists.'))}${!rs.some(r=>r.status==='verified')&&a.kind!=='stock'?`<p class="hint">${tr('想看与股票有真实配对的资产？','Looking for assets with real stock pairs?')} <a href="#meme?filter=related">${tr('Meme 雷达 · 重点关系','Meme Radar · priority relations')} →</a> · <a href="#pair">${tr('交易池分析','Pool analysis')}</a></p>`:''}</section><section class="panel x-ai"><h2>${tr('数据解读','Data readout')}</h2><p class="hint">${tr('由 DeepSeek 基于本站已采集字段生成，只解读已有数据，不预测价格。','Generated by DeepSeek from collected fields only; no price forecasts.')}</p><div class="v2-ai-text" id="v2Insight" data-chain="${chain(a)}" data-token="${esc(a.token)}"></div></section>
      <div class="kpis">${card(tip('price',tr('最新价格','Latest price')),field(a,'price',true),age(a.fieldTimes?.price??a.updatedAt),'','data-kpi="price"')}${card(tip('volume',tr('24h 成交额','24h volume')),field(a,'volume24h',true),a.volumeScope==='exchange'?tr('场内统计','Exchange statistics'):'DEX')}${card(tip('trades',tr('24h 成交次数','24h trades')),(a.buys24h!=null&&a.sells24h!=null)?num((a.buys24h??0)+(a.sells24h??0)):field(a,'txs24h'),(a.buys24h!=null&&a.sells24h!=null)?tr('买 / 卖（同口径合计）：','Buys / sells (same scope): ')+num(a.buys24h)+' / '+num(a.sells24h):tr('总次数（无拆分，批量行情口径）','Total only (batch quote scope)'))}${card(tip('holders',tr('持币地址数','Holder addresses')),field(a,'holders'),age(a.fieldTimes?.holders))}</div>
      <div class="x-grid"><section class="panel"><div class="panel-head"><h2>${tr('价格走势','Price history')}${tip('prices','')}</h2><div class="tab-group" id="v2ChartTabs"><button class="tab" data-chart-mode="line">${tr('分时','Line')}</button><button class="tab" data-chart-mode="candle">${tr('K线','Candles')}</button>${chartMode==='candle'?['1m','5m','15m','1H'].map(b=>`<button class="tab" data-chart-bar="${b}">${b}</button>`).join(''):''}</div></div><div id="v2Price" class="chart x-candles"></div><p class="hint" id="v2ChartHint"></p></section><section class="panel"><h2>${tip('holder',tr('前10大地址持仓占比','Top-10 address share'))}</h2>${a.risk?.top10!=null?`<strong class="kpi-value">${num(a.risk.top10)}%</strong><progress max="100" value="${Math.max(0,Math.min(100,a.risk.top10))}"></progress><p>${date(a.risk.checkedAt)} · OKX</p>`:empty(tr('上游尚未提供集中度数据。','Holder concentration is not available from the source yet.'))}<p>${tr('规则解读：先核验关系，再结合资金规模和成交覆盖判断。','Rule-based reading: verify the relationship, then examine liquidity size and activity coverage.')}</p></section></div>
      <section class="panel"><h2>${tr('最近采集的成交','Recently collected trades')}${tip('activity','')}</h2><p id="v2TradeStat">${tradeStatText(a)}</p>${d.trades?.length?`<div class="scroll"><table class="tbl"><thead><tr>${[tr('时间','Time'),tr('方向','Side'),tr('美元成交额','USD volume'),'DEX',tr('交易','Transaction')].map(t=>`<th>${t}</th>`).join('')}</tr></thead><tbody id="v2TradeBody">${d.trades.map(t=>tradeRow(t,a)).join('')}</tbody></table></div>`:empty(tr('当前保存范围内暂无成交记录。','No trades in the saved coverage.'))}</section><section class="panel"><h2>${tr('配对发现与核验记录','Discovery and verification log')}${tip('timeline','')}</h2>${(d.events??[]).map(e=>eventHtml({...e,chainId:chain(a)})).join('')||empty(tr('等待新的关系事件','Waiting for relationship events'))}</section>`;
  }
  function pairPage(stockDetail,memeDetail){
    const r=route(),c=r.parts[1],address=r.parts[2],rs=(feed.relations??[]).filter(x=>chain(x)===c&&x.stock===address),selected=rs.find(x=>x.pool===r.q.get('pool'))??rs[0];
    const s=(feed.stockTokens??[]).find(x=>chain(x)===c&&x.tokenContractAddress?.toLowerCase()===address),a=memeDetail?.asset;
    const pools=(memeDetail?.pools??[]).filter(p=>fresh(p.checkedAt)),total=pools.reduce((n,p)=>n+(p.liquidityUsd??0),0);
    return `<div class="page-heading"><h2>${selected?esc((feed.assets??[]).find(a=>a.token===selected.token&&chain(a)===c)?.symbol??short(selected.token))+' / ':''}${esc(s?.tokenSymbol??short(address))} · ${tr('交易池分析','Pool analysis')} ${help('pair')}</h2><p>${chainNames[c]} · <code>${esc(address)}</code> <button data-copy="${esc(address)}">${tr('复制合约地址','Copy contract')}</button></p></div><div class="v2-pair-layout"><aside class="panel"><h3>${tr('关联配对','Related pairs')}</h3>${rs.map(p=>`<a class="x-signal ${p===selected?'selected':''}" href="${pairLink(p)}"><strong>${esc((feed.assets??[]).find(a=>a.token===p.token&&chain(a)===c)?.symbol??short(p.token))}</strong><small>${esc(p.protocol)} · ${usd(p.liquidityUsd)}</small></a>`).join('')||empty(tr('尚无已核验配对','No verified pair yet'))}<a href="#stock?q=${encodeURIComponent(s?.stockCode??'')}">← ${tr('查看全部关联资产','All related assets')}</a></aside><div>${selected?`<section class="panel">${proof(selected,true)}<a href="#detail/${c}/${selected.token}">${tr('查看 Meme 详情','View Meme detail')} →</a></section><section class="panel"><h2>${tr('核心事实','Key facts')}</h2><div class="kpis">${card(tr('当前池流动性','Current pool liquidity'),usd(selected.liquidityUsd),age(selected.liquidityAt??selected.checkedAt))}${card(tr('该池 24h 成交额','Pool 24h volume'),usd(selected.poolMarket?.volume24h),selected.poolMarket?.provider?esc(selected.poolMarket.provider)+' · '+tr('池级口径','pool scope'):tr('池级口径，缺失则未采集','pool scope; missing if uncollected'))}${card(tr('买卖次数 24h','Trades 24h'),selected.poolMarket?.txs24h!=null?num(selected.poolMarket.txs24h):tr('未提供','Unavailable'),tr('池级口径','pool scope'))}</div></section><section class="panel"><h2>${tip('premium',tr('链上价 vs 股价','On-chain price vs stock'))}</h2>${s?.stockPrice!=null?`<div class="kpis">${card(tip('premium',tr('真实股价参考','Stock reference')),usd(s?.stockPrice),tr('标的层','Underlying'))}${card(tip('price',tr('股票代币价','Stock-token price')),usd(s?.price),age(s?.updatedAt))}${card(tip('premium',tr('链上价 vs 股价','on-chain vs stock')),pct(s?.premium?.value),s?.premium?.reason?tr('缺少对齐价格或换算比例','Aligned prices or conversion ratio unavailable'):tr('按每枚对应股数换算','Adjusted for shares per token'))}</div>`:`<p class="hint">${tr('该股票暂缺独立参考股价（独立行情源按日轮换覆盖中），链上价照常显示，偏离值待参考价就绪后自动计算。','No independent stock quote for this ticker yet (rolling coverage); on-chain price is shown and deviation computes once a reference arrives.')}</p>`}</section><section class="panel"><h2>${tr('流动性池分布','Pool distribution')}${tip('pools','')}</h2><p>${tr('池规模与费率供参考；不是锁仓证明或 LP 收益率。占比仅以本次新鲜的已覆盖池为分母。','Pool size and fees are references, not proof of locking or LP returns. Shares use only fresh covered pools.')}</p>${pools.length?pools.map(p=>`<div class="v2-pool"><strong>${esc(p.name??p.protocol)}</strong> ${usd(p.liquidityUsd)} · ${tr('费率','Fee')} ${p.feePct==null?tr('未提供','Unavailable'):num(p.feePct)+'%'}<progress max="100" value="${total>0?100*(p.liquidityUsd??0)/total:0}"></progress><small>${(p.amounts??[]).map(x=>esc(x.tokenSymbol)+': '+num(x.tokenAmount)).join(' / ')} · ${age(p.checkedAt)}</small></div>`).join(''):empty(tr('新鲜池结构待采集，已有关系证据保留在上方。','Fresh pool structure is pending; saved relationship evidence remains above.'))}</section><div class="x-grid"><section class="panel"><h2>${tr('Meme 与股票代币涨跌对比','Meme vs stock-token returns')}${tip('comparison','')}</h2><select id="v2Window" aria-label="${tr('时间窗口','Time window')}">${['1h','24h','7d'].map(v=>`<option ${r.q.get('window')===v||!r.q.has('window')&&v==='24h'?'selected':''}>${v}</option>`).join('')}</select><div id="v2Comparison" class="chart"></div><p class="hint">${tr('仅展示共同采样时点。归一化走势不是价差或因果关系；缺少独立池外报价时不计算池隐含偏离。','Only shared observation times are plotted. Normalized returns are not spreads or causality; pool-implied deviation requires independent external quotes.')}</p></section><section class="panel"><h2>${tip('holder',tr('前10大地址持仓占比','Top-10 address share'))}</h2>${a?.risk?.top10!=null?`<strong class="kpi-value">${num(a.risk.top10)}%</strong><progress max="100" value="${Math.max(0,Math.min(100,a.risk.top10))}"></progress><p>${date(a.risk.checkedAt)}</p>`:empty(tr('持仓集中度待采集','Holder concentration pending'))}<h3>${tr('交易活动','Trading activity')}</h3><p>24h ${usd(a?.volume24h)} · ${num(a?.holders)} ${tr('持有人','holders')}</p>${tr('活跃度评分数据不足','Activity score lacks inputs')}</section></div>`:''}</div></div>`;
  }
  function draw(id,series){const el=document.getElementById(id);if(!el)return;const ready=series.filter(s=>s.data.length>=2);if(!ready.length){el.innerHTML=empty(tr('共同样本不足，等待后续采集。','Not enough common samples; waiting for collection.'));return;}if(!window.echarts)return;const c=echarts.init(el);charts.push(c);c.setOption({tooltip:{trigger:'axis'},legend:{textStyle:{color:'#a4b2c5'}},grid:{left:65,right:20,top:40,bottom:30},xAxis:{type:'time'},yAxis:{type:'value',scale:true},series:ready.map(s=>({...s,type:'line',showSymbol:false,connectNulls:false}))});}
  function plotPair(sd,md){const windowMs={'1h':3600000,'24h':86400000,'7d':604800000}[route().q.get('window')??'24h']??86400000;const right=new Map((sd?.samples??[]).map(p=>[p.t,p.price]));const common=(md?.samples??[]).filter(p=>p.t>=Date.now()-windowMs&&p.price>0&&(right.get(p.t)??0)>0);const first=common[0];draw('v2Comparison',first?[{name:tr('Meme 变化 %','Meme return %'),data:common.map(p=>[p.t,100*(p.price/first.price-1)])},{name:tr('股票代币变化 %','Stock-token return %'),data:common.map(p=>[p.t,100*(right.get(p.t)/right.get(first.t)-1)])}]:[]);}
  async function loadEvents(more=false){if(eventBusy)return;eventBusy=true;eventError=false;try{const responses=await Promise.all(Object.keys(chainNames).filter(c=>!more||eventCursors[c]!==null).map(async c=>{const response=await fetch('/api/events?chain='+c+(more&&eventCursors[c]?'&before='+encodeURIComponent(JSON.stringify(eventCursors[c])):''));if(!response.ok)throw new Error('events');return {c,data:await response.json()};}));if(!more)eventRows=[];for(const {c,data}of responses){eventRows.push(...data.items.map(e=>({...e,chainId:c})));eventCursors[c]=data.next;}eventRows=[...new Map(eventRows.map(e=>[chain(e)+':'+e.id,e])).values()].sort((a,b)=>b.t-a.t);eventsLoaded=true;}catch{eventError=true;}finally{eventBusy=false;if(route().parts[0]==='events')render(snapshot);}}
  function preserve(el,html){const open=new Set([...el.querySelectorAll('details[open][data-open-id]')].map(d=>d.dataset.openId));const a=document.activeElement,id=a?.id,pos=a?.selectionStart;el.innerHTML=html;el.querySelectorAll('details[data-open-id]').forEach(d=>{if(open.has(d.dataset.openId))d.open=true;});if(id){const next=document.getElementById(id);if(next){next.focus({preventScroll:true});try{next.setSelectionRange(pos,pos);}catch{}}}}
  // Cells and KPIs that changed since the last poll get a one-shot flash so
  // live price movement is visible even though rows are re-rendered.
  let lastCellVals=new Map(),lastKpiVals=new Map();
  function flashChanged(){
    const next=new Map(),pending='待采集';
    document.querySelectorAll('tr[data-asset] td[data-field]').forEach(td=>{
      const k=td.closest('tr').dataset.asset+':'+td.dataset.field,t=td.textContent.trim();
      const prev=lastCellVals.get(k);
      if(prev!==undefined&&prev!==t&&t&&!t.includes(pending))td.classList.add('kpi-flash');
      next.set(k,t);
    });
    lastCellVals=next;
    const kn=new Map();
    document.querySelectorAll('.kpi[data-kpi-key] .kpi-value').forEach(el=>{
      const k=el.closest('.kpi').dataset.kpiKey,t=el.textContent.trim();
      const prev=lastKpiVals.get(k);
      if(prev!==undefined&&prev!==t&&t)el.classList.add('kpi-flash');
      kn.set(k,t);
    });
    lastKpiVals=kn;
  }
  // SSE client over fetch-streaming: nginx sub_filter rewrites fetch('/api/'
  // literals but not EventSource URLs, so read the stream manually. Polling
  // remains the fallback whenever the stream is down.
  function handleStreamEvent(event,raw){
    let d;try{d=JSON.parse(raw);}catch{return;}
    if(event==='price'&&d.token&&d.price!=null){
      const a=(feed.assets??[]).find(x=>x.token===d.token&&String(x.chainId??x.chain)==='196');
      if(a){a.price=d.price;a.fieldTimes={...(a.fieldTimes||{}),price:d.at};}
      if(lastDetail&&lastDetail.asset?.token===d.token){
        lastDetail.asset.price=d.price;
        const priceEl=document.querySelector('#memeDetail [data-kpi="price"] .kpi-value');
        if(priceEl){const t=usd(d.price);if(t!==lastPriceText){priceEl.textContent=t;priceEl.classList.remove('kpi-flash');void priceEl.offsetWidth;priceEl.classList.add('kpi-flash');}lastPriceText=t;}
        void paintDetailChart(lastDetail);
      }
      if(a)document.querySelectorAll(`tr[data-asset="${d.token}"] td[data-field="price"]`).forEach(td=>{
        const before=td.textContent;td.innerHTML=field(a,'price',true);
        if(td.textContent!==before){td.classList.remove('kpi-flash');void td.offsetWidth;td.classList.add('kpi-flash');}
      });
    }else if(event==='trade'&&d.token&&Array.isArray(d.fresh)){
      if(lastDetail&&lastDetail.asset?.token===d.token){
        const a=lastDetail.asset,tbody=document.getElementById('v2TradeBody');
        if(tbody){
          const unseen=d.fresh.filter(t=>!lastTradeIds.has(t.id));
          if(unseen.length){
            tbody.insertAdjacentHTML('afterbegin',unseen.map(t=>`<tr class="trade-new">${tradeRow(t,a).slice(3)}`).join(''));
            while(tbody.children.length>150)tbody.lastElementChild.remove();
            unseen.forEach(t=>lastTradeIds.add(t.id));
          }
        }
      }
    }
  }
  async function connectStream(){
    for(;;){
      try{
        const res=await fetch('/api/stream');
        if(!res.ok||!res.body)throw new Error('stream unavailable');
        const reader=res.body.getReader(),dec=new TextDecoder();let buf='';
        for(;;){
          const {done,value}=await reader.read();
          if(done)break;
          buf+=dec.decode(value,{stream:true});
          let idx;
          while((idx=buf.indexOf('\n\n'))>=0){
            const frame=buf.slice(0,idx);buf=buf.slice(idx+2);
            let event='message',data='';
            for(const line of frame.split('\n')){
              if(line.startsWith('event:'))event=line.slice(6).trim();
              else if(line.startsWith('data:'))data+=line.slice(5).trim();
            }
            if(data&&event!=='heartbeat')handleStreamEvent(event,data);
          }
        }
      }catch(e){/* stream down; polling keeps the page live */}
      await new Promise(r=>setTimeout(r,5000));
    }
  }
  function render(data){if(!data)return;loadRegistry();snapshot=data;feed=data.unified??data.xlayer??{};const byId=new Map((feed.assets??[]).map(a=>[key(a),a]));feed={...feed,groups:(feed.groups??[]).map(g=>({...g,members:g.members.map(a=>typeof a==='string'?byId.get(a):a).filter(Boolean)}))};summarySources();
    for(const id of ['meme','stock']){const h=document.querySelector('#view-'+id+' > .page-heading h2');if(h&&!h.querySelector('[data-help]'))h.insertAdjacentHTML('beforeend',' '+help(id));}trackListHash();setTimeout(fillAI,0);
    if(openHelp&&helpLanguage!==tr('zh','en'))showHelp(openHelp);
    document.getElementById('bnbUsd').closest('.metric').hidden=true;document.getElementById('network').value='196';
    for(const id of ['memeLegacy','stockLegacy','memeUnavailable','signalMetrics'])document.getElementById(id).hidden=true;
    for(const child of document.getElementById('view-live').children)child.hidden=true;
    let live=document.getElementById('xlayerLive');if(!live){live=document.createElement('div');live.id='xlayerLive';document.getElementById('view-live').append(live);}live.hidden=false;
    let meme=document.getElementById('xlayerMeme');if(!meme){meme=document.createElement('div');meme.id='xlayerMeme';document.getElementById('view-meme').append(meme);}
    const r=route(),page=r.parts[0];
    // The detail chart updates incrementally across ticks; disposing it here
    // would rebuild the canvas every poll. Dispose only when leaving detail.
    if(page!=='detail')killDetailChart();
    charts.forEach(c=>{if(c!==detailChart)c.dispose();});charts=[];
    const turn=++sequence;activeHash=location.hash;
    if(page==='live'){preserve(live,overview());(feed.sectors??[]).filter(s=>s.lastValue!=null||s.value!=null).forEach((s,i)=>{const el=live.querySelector(`[data-chart="basket-${i}"]`);if(el){el.id='v2Basket'+i;draw(el.id,[{name:tr(s.sector,sectorNames[s.sector]??s.sector),data:s.history.map(p=>[p.t,p.price])}]);}});}
    if(page==='meme')preserve(meme,memePage());if(page==='stock')preserve(document.getElementById('okxStocks'),stockPage());
    if(page==='events'){const el=document.getElementById('view-events');preserve(el,`<section class="panel"><h2>${tr('发现记录','Discovery log')}${tip('leads','')}</h2><p>${tr('历史持续保存，重复轮询不会生成重复线索。时间表示本站发现时间。','History persists; repeated polling does not duplicate leads. Times represent detection by this product.')}</p><button id="v2NewEvents">${tr('获取最新记录','Load latest records')}</button>${eventError?empty(tr('历史读取失败，请重试。','Could not load history; retry.')):''}${eventRows.map(eventHtml).join('')||empty(tr('正在读取历史…','Loading history…'))}<button id="v2MoreEvents" ${eventBusy||eventsLoaded&&Object.values(eventCursors).every(v=>v===null)?'disabled':''}>${tr('加载更早记录','Load older records')}</button></section>`);if(!eventsLoaded&&!eventBusy&&!eventError)void loadEvents();}
    if(page==='detail'||page==='pair'){
      const el=document.getElementById(page==='detail'?'memeDetail':'view-pair'),c=r.parts[1],address=r.parts[2]?.toLowerCase();
      if(!chainNames[c]||!/^0x[\da-f]{40}$/.test(address??'')){preserve(el,`<section class="panel"><h2>${tr('选择资产或配对','Choose an asset or pair')}${tip('pair','')}</h2><form id="v2Search"><input name="q" required placeholder="${tr('输入股票或 CA','Enter ticker or CA')}"><button>${tr('搜索','Search')}</button></form>${(feed.relations??[]).filter(r=>r.status==='verified').slice(0,30).map(r=>`<a class="x-signal" href="${pairLink(r)}">${esc(r.ticker)} · ${chainNames[chain(r)]} · ${short(r.token)} <span>${usd(r.liquidityUsd)}</span></a>`).join('')}</section>`);return;}
      const show=async()=>{try{const d=await cachedDetail(c,address);let md;
        if(page==='pair'){const rs=(feed.relations??[]).filter(x=>chain(x)===c&&x.stock===address),sel=rs.find(x=>x.pool===r.q.get('pool'))??rs[0];if(sel)md=await cachedDetail(c,sel.token);}
        if(sequence!==turn)return;
        if(page==='detail'){
          lastDetail=d;
          if(el.dataset.xAsset===d.asset?.token){updateDetailIncremental(d);return;}
          preserve(el,detailPage(d));el.dataset.xAsset=d.asset?.token??'';
          lastTradeIds=new Set((d.trades??[]).map(t=>t.id));lastPriceText=d.asset?.price!=null?usd(d.asset.price):'';
          void paintDetailChart(d,true);
        }else{preserve(el,pairPage(d,md));plotPair(d,md);}
      }catch{if(sequence===turn)preserve(el,empty(tr('该资产尚未进入可用索引，或暂时读取失败。','This asset is not yet indexed or could not be loaded.')+` <a href="#stock">${tr('返回股票雷达','Back to Stock Radar')}</a>`));}};
      if(!cache.has(c+':'+address))preserve(el,empty(tr('正在读取已保存的数据…','Loading saved data…')));void show();
    }
    const footer=document.querySelector('footer');footer.removeAttribute('data-i18n');footer.textContent=tr('关系有证据，数字有来源。历史持续保存，缺失不等于零。','Evidence for relationships. Sources for numbers. History persists; missing is not zero.');
    lastLang=typeof LANG!=='undefined'?LANG:'zh';
    flashChanged();
  }
  document.addEventListener('click',e=>{
    const cm=e.target.closest('[data-chart-mode]');
    if(cm){const m=cm.dataset.chartMode;if(m!==chartMode){chartMode=m;if(lastDetail)void paintDetailChart(lastDetail,true);}return;}
    const cb=e.target.closest('[data-chart-bar]');
    if(cb){const b=cb.dataset.chartBar;if(b!==chartBar){chartBar=b;if(lastDetail)void paintDetailChart(lastDetail,true);}return;}
  });
  document.addEventListener('click',e=>{const g=e.target.closest('[data-v2-group]');if(g){const id=g.dataset.v2Group;expandedGroups.has(id)?expandedGroups.delete(id):expandedGroups.add(id);render(snapshot);document.querySelectorAll('[data-v2-group]').forEach(b=>{if(b.dataset.v2Group===id)b.focus({preventScroll:true});});}const f=e.target.closest('[data-v2-filter]');if(f)setQuery({filter:f.dataset.v2Filter,page:null});const p=e.target.closest('[data-v2-page]');if(p)setQuery({page:Math.max(0,Number(p.dataset.v2Page))});if(e.target.id==='v2MoreEvents')void loadEvents(true);if(e.target.id==='v2NewEvents')void loadEvents(false);});
  document.addEventListener('input',e=>{if(['xMemeSearch','xStockSearch'].includes(e.target.id)&&window.RadarV2)setQuery({q:e.target.value,page:null});});
  document.addEventListener('change',e=>{const fields={v2Sort:'sort',v2Chain:'chain',v2Min:'minLiquidity',v2Window:'window'};if(fields[e.target.id])setQuery({[fields[e.target.id]]:e.target.value,page:null});});
  document.addEventListener('submit',e=>{if(e.target.id!=='v2Search')return;e.preventDefault();const value=new FormData(e.target).get('q').trim(),stocks=(feed.stockTokens??[]).filter(s=>[s.stockCode,s.tokenContractAddress,names[s.stockCode],s.stockIdentity?.nameZh,s.stockIdentity?.nameEn,s.stockIdentity?.code].filter(Boolean).some(v=>v.toLowerCase()===value.toLowerCase()));location.hash=(stocks.length?'#stock':'#meme')+'?'+new URLSearchParams({q:value,filter:'all'});});
  document.addEventListener('click',e=>{if(e.target.closest('#xStockClear')){location.hash='#stock';return;}const button=e.target.closest('[data-help]');if(button){e.preventDefault();e.stopPropagation();showHelp(button.dataset.help,button);}},true);
  document.addEventListener('keydown',e=>{if(!openHelp)return;if(e.key==='Escape'){e.preventDefault();closeHelp();}else if(e.key==='Tab'){e.preventDefault();document.querySelector('#v2HelpDialog [data-help-close]').focus();}});
  document.addEventListener('error',e=>{if(e.target.matches?.('img.v2-logo')){const span=document.createElement('span');span.className='v2-logo placeholder';span.textContent='•';e.target.replaceWith(span);}},true);
  window.addEventListener('resize',()=>{charts.forEach(c=>c.resize());if(detailChart&&detailChartEl){try{detailChart.resize(detailChartEl.clientWidth||600,detailChartEl.clientHeight||380);}catch(e){}}});
  void connectStream();
  window.RadarV2={render,marketKey:key};
})();
