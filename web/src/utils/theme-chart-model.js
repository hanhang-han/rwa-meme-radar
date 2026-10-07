const finite=value=>value!=null&&value!==''&&Number.isFinite(Number(value));
export function themeChartSeries(data = {}) {
  const clean=(rows,field)=>[...new Map((rows??[]).filter(row=>finite(row.t)&&finite(row[field])&&Number(row.t)>0&&Number(row[field])>=0).map(row=>[Number(row.t),{...row,t:Number(row.t),[field]:Number(row[field])}])).values()].sort((a,b)=>a.t-b.t);
  const prices=clean(data.prices,'value'),volumes=clean(data.volumes,'volumeUsd');
  const times=[...prices,...volumes].map(row=>row.t);
  const start=finite(data.startAt??data.from)?Number(data.startAt??data.from):times.length?Math.min(...times):null;
  const end=finite(data.endAt??data.to)?Number(data.endAt??data.to):times.length?Math.max(...times):null;
  const events=(data.events??[]).filter(row=>finite(row.t)&&Number(row.t)>0&&(start==null||Number(row.t)>=start)&&(end==null||Number(row.t)<=end)).map(row=>({...row,t:Number(row.t)})).sort((a,b)=>a.t-b.t);
  return {prices,volumes,events,start,end};
}
