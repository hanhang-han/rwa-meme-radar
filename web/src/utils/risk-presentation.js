const text=(pair,lang)=>pair[lang==='en'?1:0];
const finite=v=>v!=null&&v!==''&&(typeof v==='number'||typeof v==='string'&&/^-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(v))&&Number.isFinite(Number(v));
const names={
 buyTaxPct:['买入税率','Buy tax'],sellTaxPct:['卖出税率','Sell tax'],honeypot:['卖出限制','Selling restriction'],thresholdPct:['触发阈值','Threshold'],triggers:['触发项目','Triggered fields'],testedFields:['检测项目','Tested fields'],mintable:['可增发','Mintable'],pausable:['可暂停交易','Pausable'],blacklist:['黑名单权限','Blacklist permission'],proxy:['代理合约','Proxy contract'],openSource:['代码已公开','Open source'],ownerChangeBalance:['可修改余额','Can change balances'],canTakeBackOwnership:['可收回权限','Can reclaim ownership'],hiddenOwner:['隐藏所有者','Hidden owner'],selfDestruct:['可销毁合约','Self-destruct'],ownerAddress:['所有者地址','Owner address'],creatorAddress:['创建者地址','Creator address'],top10Percent:['前十大原始持仓占比','Raw top 10 holdings'],top10RawPercent:['前十大原始持仓占比','Raw top 10 holdings'],top10AdjustedPercent:['调整后前十大持仓占比','Adjusted top 10 holdings'],sampleHolderCount:['已观测地址数','Observed addresses'],samplePercent:['已观测持仓占比','Observed holdings'],exclusionsApplied:['已排除池子等地址','Address exclusions applied'],identified:['已识别锁定信息','Lock information identified'],pool:['交易池','Pool'],coveredPercent:['LP 持仓覆盖率','LP holder coverage'],lockedPercentMin:['至少已锁定','Minimum locked'],burnedPercent:['已销毁','Burned'],unlockedPercentMin:['至少未锁定','Minimum unlocked'],lockedPercent:['锁定占比','Locked'],unlockedPercent:['未锁定占比','Unlocked'],nextUnlockAt:['下次解锁','Next unlock'],minimumLockedPercent:['锁定与销毁合计阈值','Locked plus burned threshold'],volumeLiquidityRatio:['成交额 / 流动性','Volume / liquidity'],volumeLiquidity:['成交与流动性','Volume and liquidity'],volume24hUsd:['24h 成交额','24h volume'],totalLiquidityUsd:['总流动性','Total liquidity'],transactionsPerHolder:['每地址成交笔数','Trades per holder'],transactions24h:['24h 成交笔数','24h trades'],holderAddresses:['持币地址数','Holder addresses'],change24hPct:['24h 涨跌','24h change'],scope:['统计范围','Scope'],numeratorScope:['成交统计范围','Trading scope'],denominatorScope:['分母统计范围','Denominator scope'],holderScope:['地址范围','Holder scope'],changeScope:['涨跌范围','Return scope'],liquidityScope:['流动性范围','Liquidity scope'],exclusions:['排除地址类别','Excluded address types'],source:['来源','Source'],provider:['来源','Source'],coverage:['覆盖范围','Coverage'],volumeAt:['成交观测时间','Volume observed'],liquidityAt:['流动性观测时间','Liquidity observed'],transactionsAt:['成交笔数观测时间','Trade count observed'],holdersAt:['持仓观测时间','Holdings observed'],changeAt:['涨跌观测时间','Return observed'],checkedAt:['检测时间','Checked at'],threshold:['触发阈值','Threshold'],status:['检测状态','Status'],reason:['结果说明','Result'],evidence:['检测值','Evidence']
};
const values={
 'token':['单币','Token'],'token-aggregate':['单币多池合计','Token across pools'],'single-v2-pool':['单个 V2 池','Single V2 pool'],'token-holder-addresses':['持币地址','Holder addresses'],'provider-indexed-pools':['来源已覆盖的交易池','Pools covered by the source'],'observed-holders':['已观测持币地址','Observed holder addresses'],'provider-top10-including-pools':['原始前十大，包含池子等地址','Raw top 10 including pools'],'pools-burn-known-exchanges':['池子、销毁和已识别交易所地址','Pools, burn and known exchanges'],'provider-v2-lp-holders':['V2 池 LP 持仓','V2 pool LP holders'],
 'missing-evidence':['暂无检测数据','No check data'],'scan-trigger':['检测值触发规则','A check triggered a rule'],'scan-clear':['已检测项目未触发规则','Tested fields did not trigger a rule'],'incomplete-scan':['部分检测值缺失','Some check values are unavailable'],'stale-scan':['检测结果已过期','The check has expired'],'permission-detected':['存在需留意的合约权限','Contract permissions need attention'],'address-exclusions-unavailable':['尚不能排除池子等地址，集中度未知','Address exclusions are unavailable; concentration is unknown'],'threshold-exceeded':['超过触发阈值','Threshold exceeded'],'below-threshold':['未超过当前阈值','Below the current threshold'],'lp-lock-observed':['已识别锁定或销毁的 LP 份额','Locked or burned LP shares identified'],'lp-unlocked-observed':['已识别未锁定的 LP 份额','Unlocked LP shares identified'],'lp-coverage-incomplete':['LP 持仓覆盖不足','LP holder coverage is incomplete'],'lp-lock-expired':['锁定记录已到期','The lock record has expired'],'lp-evidence-unavailable':['暂无可用 LP 锁定数据','LP lock data is unavailable'],'lp-pool-ambiguous':['锁定数据无法对应到单个池','Lock data cannot be matched to one pool'],'lp-type-unsupported':['此池类型暂无锁定数据','Lock data is unavailable for this pool type'],'incomplete-or-incompatible-market-coverage':['成交与流动性覆盖不足','Trading and liquidity coverage is incomplete'],
 'unknown':['未能识别','Unknown'],'clear':['未触发','No flag'],'triggered':['需留意','Attention']
};
export function riskEvidenceLabel(key,lang='zh'){return text(names[key]??['其他检测值','Other check value'],lang);}
export function riskStateText(status,lang='zh'){return text(values[['clear','triggered'].includes(status)?status:'unknown'],lang);}
export function riskReasonText(reason,lang='zh'){return text(values[reason]??values['missing-evidence'],lang);}
export function riskEvidenceValue(key,value,lang='zh'){
 if(value==null||value==='')return text(values.unknown,lang);
 if(typeof value==='boolean')return text(value?['是','Yes']:['否','No'],lang);
 if(Array.isArray(value))return value.length?value.map(v=>names[v]?riskEvidenceLabel(v,lang):riskEvidenceValue('',v,lang)).join(' · '):text(['无','None'],lang);
 if(typeof value==='object')return text(values.unknown,lang);
 if(Object.hasOwn(values,value))return text(values[value],lang);
 if(/At$/.test(key)&&finite(value))return Number(value)>0?new Date(Number(value)).toLocaleString(lang==='en'?'en-US':'zh-CN',{hour12:false}):text(values.unknown,lang);
 if(finite(value)){
  const formatted=Number(value).toLocaleString('en-US',{maximumFractionDigits:2});
  if(/Pct$|Percent$|PercentMin$/.test(key))return formatted+'%';
  if(/Usd$/.test(key))return '$'+formatted;
  if(key==='volumeLiquidityRatio')return formatted+'x';
  return formatted;
 }
 return String(value);
}
export function riskEvidenceRows(evidence,lang='zh',prefix=''){
 if(!evidence||typeof evidence!=='object'||Array.isArray(evidence))return [];
 return Object.entries(evidence).flatMap(([key,value])=>{
  const label=[prefix,riskEvidenceLabel(key,lang)].filter(Boolean).join(' · ');
  return value&&typeof value==='object'&&!Array.isArray(value)?riskEvidenceRows(value,lang,label):[{key:prefix+':'+key,label,value:riskEvidenceValue(key,value,lang),title:typeof value==='string'?value:undefined}];
 });
}
export function riskFlagSuffix(flag,evidence){
 return flag==='wash_suspect'&&finite(evidence?.volumeLiquidityRatio)?`${Math.round(Number(evidence.volumeLiquidityRatio))}x`:'';
}
export function riskProvenance(check){
 const evidence=check?.evidence??{};
 return {provider:check?.provider??evidence.provider??null,at:check?.checkedAt??evidence.checkedAt??evidence.volumeAt??evidence.transactionsAt??evidence.holdersAt??null};
}
