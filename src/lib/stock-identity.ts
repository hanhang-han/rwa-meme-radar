import { readFileSync } from 'node:fs';

// The pinned xStocks API deployment list is shared with the Python read model.
// A token symbol, provider issuer string, or wrapper asset() result cannot add
// an address to this index. Only chain+contract identity can.
type Deployment = { native?:string; wrapperCurrent?:string; wrapperLegacy?:string };
type Asset = {assetId:string;ticker:string;tokenSymbol:string;nameEn:string;underlyingIsin?:string;deployments:Record<string,Deployment>};
type Manifest = {version:string;capturedAt:string;sourceUrl:string;provenance:string;assets:Asset[]};
export type TokenVerificationStatus = 'official'|'legacy'|'unverified';
export type StockTokenIdentity = {
  chainId:string;address:string|null;underlyingId:string|null;ticker:string|null;
  issuer:string|null;tokenKind:'native'|'wrapper-current'|'wrapper-legacy'|null;
  version:string|null;verificationStatus:TokenVerificationStatus;eligibleForPair:boolean;
  sourceUrl:string|null;sourceAt:string|null;manifestVersion:string|null;
  nameEn?:string;tokenSymbol?:string;underlyingIsin?:string;
};
const ADDRESS=/^0x[\da-f]{40}$/i;
const manifest:Manifest=JSON.parse(readFileSync(new URL('../catalogues/official-stock-tokens.v1.json',import.meta.url),'utf8'));
if(manifest.provenance!=='official-api-pinned-snapshot'||!manifest.version)throw new Error('Stock identity manifest provenance/version missing');
const deployments=new Map<string,StockTokenIdentity>();
for(const asset of manifest.assets)for(const [chainId,addresses] of Object.entries(asset.deployments)){
  for(const [field,kind,version] of [
    ['native','native','v2'],['wrapperCurrent','wrapper-current','v2'],['wrapperLegacy','wrapper-legacy','v1'],
  ] as const){
    const address=addresses[field];if(!address)continue;
    if(!ADDRESS.test(address))throw new Error('Invalid official deployment address');
    const key=`${chainId}:${address.toLowerCase()}`;
    if(deployments.has(key))throw new Error('Conflicting official deployment addresses');
    deployments.set(key,{chainId,address:address.toLowerCase(),underlyingId:`xstocks:${asset.assetId}`,
      ticker:asset.ticker,issuer:'xStocks',tokenKind:kind,version,
      verificationStatus:kind==='wrapper-legacy'?'legacy':'official',eligibleForPair:kind!=='wrapper-legacy',
      sourceUrl:manifest.sourceUrl,sourceAt:manifest.capturedAt,manifestVersion:manifest.version,
      nameEn:asset.nameEn,tokenSymbol:asset.tokenSymbol,underlyingIsin:asset.underlyingIsin});
  }
}
export const stockTokenManifest={version:manifest.version,sourceAt:manifest.capturedAt,sourceUrl:manifest.sourceUrl,entries:deployments.size};
export function officialStockIdentity(chainId:string|number|undefined,address:string|undefined,reportedTicker?:string):StockTokenIdentity{
  const chain=String(chainId??''),canonical=ADDRESS.test(address??'')?address!.toLowerCase():null;
  const known=canonical?deployments.get(`${chain}:${canonical}`):undefined;
  if(known)return {...known};
  return {chainId:chain,address:canonical,underlyingId:null,ticker:null,issuer:null,tokenKind:null,version:null,
    verificationStatus:'unverified',eligibleForPair:false,sourceUrl:null,sourceAt:null,manifestVersion:manifest.version,
    ...(reportedTicker?{reportedTicker:reportedTicker.trim().toUpperCase()}:{}),
  };
}

// OKX reports CRDA for the CRDAx native deployment, while the issuer's
// address-verified asset record names its underlying CRDAL (ISIN GB00BJFFLV09).
// Keep this correction narrow: a matching name alone must never relabel a
// different token, and an unexpected provider code remains visible for review.
export function canonicalStockCode(row:{chainId?:string|number;chainIndex?:string|number;tokenContractAddress?:string;stockCode?:string;tokenSymbol?:string}){
  const reported=String(row.stockCode??'').trim().toUpperCase();
  const trust=officialStockIdentity(row.chainId??row.chainIndex,row.tokenContractAddress,reported);
  if(reported==='CRDA' && trust.underlyingId==='xstocks:fe08dada-99dd-4d75-b580-07c63b1eeabd'
    && trust.ticker==='CRDAL' && trust.tokenSymbol==='CRDAx' && trust.underlyingIsin==='GB00BJFFLV09'
    && String(row.tokenSymbol??'').trim().toUpperCase()==='CRDAX'){
    return {stockCode:'CRDAL',reportedStockCode:reported,stockCodeSource:'official-xstocks-manifest',
      stockCodeSourceUrl:trust.sourceUrl};
  }
  return {stockCode:reported};
}

// These mappings identify the underlying named by the provider, not the issuer's
// authorisation of a particular on-chain contract. Do not infer a market from digits alone.
const hk:Record<string,{symbol:string;zh:string;en:string;source:string}>={
  '1':{symbol:'CKHUTX',zh:'长江和记实业',en:'CK Hutchison Holdings',source:'https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0828/2025082800501.pdf'},
  '1024':{symbol:'KUAIX',zh:'快手',en:'Kuaishou Technology',source:'https://ir.kuaishou.com/node/6441/pdf'},
  '1038':{symbol:'CKINFX',zh:'长江基建集团',en:'CK Infrastructure Holdings',source:'https://www1.hkexnews.hk/listedco/listconews/sehk/2022/0408/2022040800581.pdf'},
};
// Explicit underlying mappings, never infer a US listing from arbitrary letters.
// The mapping identifies the security, not authorisation of a token contract.
export const US_SECURITIES:Record<string,string>={
  AAPL:'Apple Inc.',TSLA:'Tesla Inc.',NVDA:'NVIDIA Corporation',GOOG:'Alphabet Inc. Class C',GOOGL:'Alphabet Inc. Class A',
  AMZN:'Amazon.com Inc.',META:'Meta Platforms Inc.',MSFT:'Microsoft Corporation',AMD:'Advanced Micro Devices Inc.',INTC:'Intel Corporation',
  COIN:'Coinbase Global Inc.',PLTR:'Palantir Technologies Inc.',SOFI:'SoFi Technologies Inc.',HIMS:'Hims & Hers Health Inc.',GME:'GameStop Corp.',
  AMC:'AMC Entertainment Holdings Inc.',DJT:'Trump Media & Technology Group Corp.',MSTR:'Strategy Inc.',RIVN:'Rivian Automotive Inc.',LCID:'Lucid Group Inc.',
  OPEN:'Opendoor Technologies Inc.',AI:'C3.ai Inc.',NKE:'NIKE Inc.',DIS:'The Walt Disney Company',NFLX:'Netflix Inc.',BA:'The Boeing Company',
  F:'Ford Motor Company',GM:'General Motors Company',SBUX:'Starbucks Corporation',MCD:'McDonald’s Corporation',NIO:'NIO Inc. ADR',
  XPEV:'XPeng Inc. ADR',LI:'Li Auto Inc. ADR',BABA:'Alibaba Group Holding ADR',JD:'JD.com Inc. ADR',PDD:'PDD Holdings Inc. ADR',
  BIDU:'Baidu Inc. ADR',NTES:'NetEase Inc. ADR',TME:'Tencent Music Entertainment ADR',IQ:'iQIYI Inc. ADR',HOOD:'Robinhood Markets Inc.',
  QQQ:'Invesco QQQ Trust',SLV:'iShares Silver Trust',SPY:'SPDR S&P 500 ETF Trust',GLD:'SPDR Gold Shares',
  TSM:'Taiwan Semiconductor Manufacturing ADR',NOK:'Nokia Oyj ADR',MRNA:'Moderna Inc.',SNDK:'Sandisk Corporation',CRCL:'Circle Internet Group Inc.',
  SOXL:'Direxion Daily Semiconductor Bull 3X Shares',SOXS:'Direxion Daily Semiconductor Bear 3X Shares',
  TQQQ:'ProShares UltraPro QQQ',SQQQ:'ProShares UltraPro Short QQQ',BMNR:'BitMine Immersion Technologies Inc.',
};
export function stockIdentity(row:{stockCode?:string;tokenSymbol?:string;assetId?:string;chainId?:string;chainIndex?:string;tokenContractAddress?:string}){
  const trust=officialStockIdentity(row.chainId??row.chainIndex,row.tokenContractAddress,row.stockCode);
  const raw=String(trust.ticker??row.stockCode??'').trim().toUpperCase(),code=/^\d+$/.test(raw)?String(Number(raw)):raw;
  const entry=hk[code];
  if(entry&&row.tokenSymbol?.toUpperCase()===entry.symbol)return {id:'XHKG:'+code.padStart(5,'0'),code:code.padStart(5,'0'),market:'HKEX',nameZh:entry.zh,nameEn:entry.en,status:'identified',source:entry.source,...trust};
  if(US_SECURITIES[code])return {id:code,code,market:'US',currency:'USD',nameZh:null,nameEn:US_SECURITIES[code],status:'identified',source:'curated-underlying-market-v1',...trust};
  if(/^\d+$/.test(raw))return {id:'unresolved:'+row.assetId,code:raw,market:null,nameZh:null,nameEn:trust.nameEn??null,status:'unresolved',source:null,...trust};
  return {id:raw||row.assetId,code:raw,market:null,nameZh:null,nameEn:trust.nameEn??null,status:'provider-code',source:null,...trust};
}
