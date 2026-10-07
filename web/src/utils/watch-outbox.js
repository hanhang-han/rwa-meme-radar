// Each tab writes immutable intents and acknowledgements. No tab replaces
// another tab's unsent edits. The latest intent survives a late older PATCH.
export const OUTBOX_PREFIX = 'cliperx-watch-outbox-v2:';
export const validWatchKey = key => /^(stock:[A-Z0-9.^-]{1,24}|[1-9]\d{0,9}:0x[a-f0-9]{40})$/.test(key);
export function createWatchOutbox({ storage = () => globalThis.localStorage, tabId = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2), now = Date.now } = {}) {
  const memory = new Map(); let sequence = 0;
  const prefix = user => `${OUTBOX_PREFIX}${encodeURIComponent(user)}:`;
  function records(user) {
    const result = new Map(memory); const target = prefix(user);
    try { const s=storage(); for(let i=0;i<s.length;i++){const key=s.key(i);if(!key?.startsWith(target))continue;try{const value=JSON.parse(s.getItem(key));if(value&&['intent','ack'].includes(value.kind)&&typeof value.add==='boolean'&&typeof value.id==='string'&&validWatchKey(value.key)&&Number.isSafeInteger(value.clock)&&value.clock>=0)result.set(key,value);}catch{/* Ignore only this malformed entry. */}} } catch {}
    return [...result].filter(([key])=>key.startsWith(target));
  }
  function latest(user, snapshot = records(user)) {
    const intents=new Map(),acks=new Map();
    for(const [,record] of snapshot){const map=record.kind==='ack'?acks:intents,previous=map.get(record.key);if(!previous||record.clock>previous.clock||record.clock===previous.clock&&record.id>previous.id)map.set(record.key,record);}
    return {intents,acks};
  }
  function write(user,key,add,kind='intent',operation) {
    const clock=Math.max(Number(now())||1,...records(user).map(([,r])=>r.clock+1));
    const id=`${tabId}-${++sequence}`,record={kind,id,key,add:!!add,clock,...(operation?{operation}:{})},name=prefix(user)+id;
    try{storage().setItem(name,JSON.stringify(record));}catch{memory.set(name,record);}
    return record;
  }
  function pending(user, snapshot) {
    const {intents,acks}=latest(user,snapshot),claims=[];
    for(const [key,intent]of intents){const ack=acks.get(key);if(!ack||ack.operation!==intent.id||ack.clock<intent.clock||ack.add!==intent.add)claims.push(intent);}
    return {add:claims.filter(x=>x.add).map(x=>x.key),remove:claims.filter(x=>!x.add).map(x=>x.key),claims};
  }
  function compact(user) {
    const snapshot=records(user),{intents,acks}=latest(user,snapshot),keep=new Set([...intents.values(),...acks.values()].map(r=>r.id));
    // A peer may write after this snapshot. Delete only immutable ids we saw;
    // a second storage enumeration must never sweep up its newer intent.
    for(const [key,r]of snapshot)if(!keep.has(r.id)){memory.delete(key);try{storage().removeItem(key);}catch{}}
  }
  function migrate(user) {
    const key=`cliperx-watch-pending:${user}`;
    try{
      const raw=storage().getItem(key);if(!raw)return;
      const old=JSON.parse(raw),changes=new Map();
      for(const asset of old.add??[])if(validWatchKey(asset))changes.set(asset,true);
      for(const asset of old.remove??[])if(validWatchKey(asset))changes.set(asset,false);
      for(const [asset,add]of changes){
        if(latest(user).intents.has(asset))continue;
        // Migration is older than every real edit, including one another tab
        // writes between this check and our storage write. Its id is idempotent.
        const id=`legacy-${encodeURIComponent(asset)}`,record={kind:'intent',id,key:asset,add,clock:1},name=prefix(user)+id;
        try{storage().setItem(name,JSON.stringify(record));}catch{memory.set(name,record);}
      }
      if(storage().getItem(key)===raw)storage().removeItem(key);
    }catch{}
  }
  return { record(user,key,add){if(user&&validWatchKey(key))return write(user,key,add);},pending,
    checkpoint(user){const snapshot=records(user),{intents}=latest(user,snapshot);return {intents:new Map([...intents].map(([key,intent])=>[key,intent.id])),pending:new Set(pending(user,snapshot).claims.map(intent=>intent.id))};},
    acknowledge(user,claims){for(const intent of claims)write(user,intent.key,intent.add,'ack',intent.id);compact(user);},
    apply(user,items,checkpoint){
      const next=new Set(items),snapshot=records(user),changes=pending(user,snapshot);for(const key of changes.add)next.add(key);for(const key of changes.remove)next.delete(key);
      // A response can contain a list captured before another tab saved its edit.
      // Overlay only this request's pending or subsequent intents, including ACKed
      // ones. Normal reads never permanently overlay already confirmed changes.
      if(checkpoint)for(const [key,intent]of latest(user,snapshot).intents)if(checkpoint.intents.get(key)!==intent.id||checkpoint.pending.has(intent.id))intent.add?next.add(key):next.delete(key);
      return next;
    },
    migrate, compact, matches(key,user){return !!user&&String(key??'').startsWith(prefix(user));} };
}
