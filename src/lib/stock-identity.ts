// These mappings identify the underlying named by the provider, not the issuer's
// authorisation of a particular on-chain contract. Do not infer a market from digits alone.
const hk:Record<string,{symbol:string;zh:string;en:string;source:string}>={
  '1':{symbol:'CKHUTX',zh:'长江和记实业',en:'CK Hutchison Holdings',source:'https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0828/2025082800501.pdf'},
  '1024':{symbol:'KUAIX',zh:'快手',en:'Kuaishou Technology',source:'https://ir.kuaishou.com/node/6441/pdf'},
  '1038':{symbol:'CKINFX',zh:'长江基建集团',en:'CK Infrastructure Holdings',source:'https://www1.hkexnews.hk/listedco/listconews/sehk/2022/0408/2022040800581.pdf'},
};
export function stockIdentity(row:{stockCode?:string;tokenSymbol?:string;assetId?:string}){
  const raw=String(row.stockCode??'').trim(),code=/^\d+$/.test(raw)?String(Number(raw)):raw;
  const entry=hk[code];
  if(entry&&row.tokenSymbol?.toUpperCase()===entry.symbol)return {id:'XHKG:'+code.padStart(5,'0'),code:code.padStart(5,'0'),market:'HKEX',nameZh:entry.zh,nameEn:entry.en,status:'identified',source:entry.source};
  if(/^\d+$/.test(raw))return {id:'unresolved:'+row.assetId,code:raw,market:null,nameZh:null,nameEn:null,status:'unresolved',source:null};
  return {id:raw||row.assetId,code:raw,market:null,nameZh:null,nameEn:null,status:'provider-code',source:null};
}
