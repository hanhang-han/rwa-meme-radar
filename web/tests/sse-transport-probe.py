"""Read-only live SSE probe: no cursor, no snapshot, no historical replay."""
import collections,json,os,subprocess,sys,tempfile,time
label,url=sys.argv[1:3]
seconds=int(sys.argv[3]) if len(sys.argv)>3 else 25
started=time.time()
header_file=tempfile.NamedTemporaryFile(prefix='cliperx-sse-headers-',delete=False)
header_file.close()
encoding_args=[] if '--identity' in sys.argv else ['--compressed']
p=subprocess.Popen(['curl','--no-buffer','--silent','--show-error','--dump-header',header_file.name,'--max-time',str(seconds),*encoding_args,url],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
buffer=b''; frames=[]; first=None; total=0
while True:
 data=os.read(p.stdout.fileno(),65536)
 if not data: break
 read_at=time.time()*1000; total+=len(data)
 if first is None: first=read_at
 buffer+=data.replace(b'\r\n',b'\n')
 while b'\n\n' in buffer:
  raw,buffer=buffer.split(b'\n\n',1); event='message'; ident=None; body=[]
  for line in raw.split(b'\n'):
   if line.startswith(b'event:'):event=line[6:].strip().decode()
   elif line.startswith(b'id:'):ident=line[3:].strip().decode()
   elif line.startswith(b'data:'):body.append(line[5:].strip())
  try: obj=json.loads(b'\n'.join(body))
  except Exception: continue
  row={'event':event,'id':ident,'readAt':read_at,'bytes':len(raw)+2}
  if isinstance(obj,dict):
   for k in ['sourceEventAt','receivedAt','persistedAt','revision','now','cursor','venue','chainId','token','marketId','poolId','bar']:
    if k in obj:row[k]=obj[k]
  frames.append(row)
p.wait()
headers={}
for line in open(header_file.name):
 if ':' in line:
  key,value=line.split(':',1)
  if key.lower() in ('content-encoding','content-type','cache-control','transfer-encoding','x-accel-buffering','vary','date'):headers[key.lower()]=value.strip()
os.unlink(header_file.name)
def stats(rows):
 vals=sorted(rows)
 if not vals:return {'n':0}
 return {'n':len(vals),'p50':vals[len(vals)//2],'p95':vals[min(len(vals)-1,int(len(vals)*.95))],'max':vals[-1]}
counts=collections.Counter(r['event'] for r in frames)
sizes=collections.Counter()
for r in frames:sizes[r['event']]+=r['bytes']
print(json.dumps({'path':label,'url':url,'requestCompressed':bool(encoding_args),'headers':headers,'startedAt':started*1000,'endedAt':time.time()*1000,'firstByteMs':first-started*1000 if first else None,'counts':dict(counts),'sizes':dict(sizes),'totalBytes':total,'partialBytes':len(buffer),'persistedToRead':stats([r['readAt']-r['persistedAt'] for r in frames if r.get('persistedAt')]),'exitCode':p.returncode,'frames':frames},indent=2))
