const norm=value=>String(value??'').toLowerCase();
export const candleKey=({chainId,token,venue='dex',marketId='',poolId='',bar})=>`${chainId}:${norm(token)}:${norm(venue)}:${norm(poolId||marketId)}:${bar}`;
export function snapshotCandleRows(response){
  // A history response is already a server observation. Preserve its watermark
  // so an older queued WS event cannot overwrite a fresher REST open candle.
  // _snapshotAt is not presented to users as a trade/source timestamp.
  const watermark=Number(response?.lastObservationAt ?? response?.lastHistoryRequestAt ?? response?.lastSourceEventAt ?? response?.lastSuccessfulAt ?? (Number(response?.at)*1000))||0;
  return (response?.rows??[]).map(row=>({...row,...(!row.observedAt&&!row.sourceEventAt&&watermark?{_snapshotAt:watermark}:{})}));
}
export function mergeCandleRows(previous,incoming,limit=1000){
  const rows=new Map(previous.map(r=>[Number(r.t),r]));
  for(const row of incoming){
    if(!row||!Number.isFinite(Number(row.t))||!['o','h','l','c'].every(k=>Number.isFinite(Number(row[k]))))continue;
    const old=rows.get(Number(row.t));
    const version=r=>Number(r.revision)||(Math.max(Number(r.sourceEventAt)||0,Number(r.observedAt)||0,Number(r._snapshotAt)||0)||Number(r.receivedAt)||0);
    if(old&&version(old)>version(row)&&version(row))continue;
    if(old?.confirmed&&!row.confirmed&&version(row)<=version(old))continue;
    rows.set(Number(row.t),{...old,...row,t:Number(row.t)});
  }
  return [...rows.values()].sort((a,b)=>a.t-b.t).slice(-limit);
}
export function candlePacketMatches(packet,selection){
  if(String(packet.chainId)!==String(selection.chainId)||norm(packet.token)!==norm(selection.token)||norm(packet.venue)!==norm(selection.venue)||packet.bar!==selection.bar)return false;
  if(selection.poolId)return norm(packet.poolId??packet.pool??packet.marketId)===norm(selection.poolId);
  if(packet.poolId||packet.pool)return false;
  return !selection.marketId||norm(packet.marketId)===norm(selection.marketId);
}

// HTTP history and live packets obey the same market boundary. Older aggregate
// responses may omit identity fields; an explicit conflicting field is never
// accepted into the selected market's cache.
export function candleResponseMatches(response,selection){
  if(response.chainId!=null&&String(response.chainId)!==String(selection.chainId))return false;
  if(response.token!=null&&norm(response.token)!==norm(selection.token))return false;
  if(response.venue!=null&&norm(response.venue)!==norm(selection.venue))return false;
  if(response.bar!=null&&response.bar!==selection.bar)return false;
  const pool=response.poolId||response.pool;
  if(selection.poolId)return norm(pool||response.marketId)===norm(selection.poolId);
  if(pool)return false;
  return !selection.marketId||!response.marketId||norm(response.marketId)===norm(selection.marketId);
}

// Receiving a historical correction proves delivery, not a fresh market trade.
// Keep market freshness and historical coverage independent of row insertion.
export function candleStreamState(previous,packet,now=Date.now()){
  const prior=previous??{};
  const sourceAt=Number(packet.sourceEventAt)||0;
  const fresh=sourceAt>0&&sourceAt<=now+1000&&now-sourceAt<=30000;
  const state=fresh?{stale:false,status:'current'}:{stale:prior.stale??true,status:prior.status??'stale'};
  state.lastSourceEventAt=Math.max(Number(prior.lastSourceEventAt)||0,sourceAt)||null;
  if(packet.poolId||packet.pool){
    state.lastTradeAt=Math.max(Number(prior.lastTradeAt)||0,Number(prior.lastSourceEventAt)||0,sourceAt)||null;
    state.marketStatus=fresh?'live':prior.marketStatus??'recovering';
    state.coverageStatus=packet.coverageStatus??prior.coverageStatus??'backfilling';
  }
  return state;
}
