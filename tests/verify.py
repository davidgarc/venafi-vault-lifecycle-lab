#!/usr/bin/env python3
"""Observe two genuine renewal cycles and report native behavior; no forced issuance."""
import concurrent.futures, datetime, json, os, pathlib, sys, time, urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import lab
ART=ROOT/'artifacts'; ART.mkdir(exist_ok=True)
def state():
    with urllib.request.urlopen('http://127.0.0.1:18080/api/state',timeout=40) as r:return json.load(r)
def identities(s,which):
    return {(p['certificate']['serial'],p['certificate']['public_key_sha256']) for p in s['experiments'][which].get('pods',[]) if p.get('certificate',{}).get('ready')}
def converged(s,which,expected):
    pods=s['experiments'].get(which,{}).get('pods',[])
    return expected>0 and len(pods)==expected and all(p.get('certificate',{}).get('valid') for p in pods) and len(identities(s,which))==1
def ready(s):
    return all(s['experiments'].get(w,{}).get('pods') and all(p.get('certificate',{}).get('valid') for p in s['experiments'][w]['pods']) for w in ('cm','agent'))
def main():
    start=state()
    if not ready(start):raise RuntimeError('Both experiment apps must be ready before verification')
    report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'profile':'actual mock validity; no forced rotation','checks':{},'rotations':{'cm':[],'agent':[]},'baseline':start}
    expected={w:start['experiments'][w].get('desired_replicas',len(start['experiments'][w]['pods'])) for w in ('cm','agent')}
    previous={w:next(iter(identities(start,w)))[0] for w in ('cm','agent')}
    certs={w:next(p['certificate'] for p in start['experiments'][w]['pods']) for w in ('cm','agent')}
    deadline=time.monotonic()+max(c['actual_duration_seconds'] for c in certs.values())+240
    sample=ART/'renewal-observations.jsonl'
    with sample.open('w') as f:
        while time.monotonic()<deadline:
            s=state();f.write(json.dumps(s)+'\n');f.flush()
            for w in ('cm','agent'):
                ids=identities(s,w)
                if converged(s,w,expected[w]):
                    serial=next(iter(ids))[0]
                    if serial!=previous[w]:
                        current=next(p['certificate'] for p in s['experiments'][w]['pods'] if p.get('certificate',{}).get('serial')==serial)
                        old=certs[w]
                        item={'previous_serial':previous[w],'serial':serial,'expected_midpoint':old['midpoint'],'new_not_before':current['not_before'],'issuance_offset_seconds':current['not_before']-old['midpoint'],'observed_at':s['observed_at'],'delivery_offset_seconds':s['observed_at']-old['midpoint'],'pods':len(s['experiments'][w]['pods']),'counters':s['mock'].get('counters',{})}
                        report['rotations'][w].append(item);previous[w]=serial;certs[w]=current
                        print(w+' observed natural rotation '+str(len(report['rotations'][w]))+' offset='+str(item['issuance_offset_seconds'])+'s',flush=True)
            if all(len(report['rotations'][w])>=2 for w in ('cm','agent')):break
            stalled=[w for w in ('cm','agent') if time.time()>certs[w]['midpoint']+120 and not converged(s,w,expected[w])]
            if stalled:
                report['native_limitations']=['Replicas failed to converge within 120 seconds after midpoint: '+','.join(stalled)]
                break
            time.sleep(8)
    end=state();report['final']=end
    for w in ('cm','agent'):
        rotations=report['rotations'][w]
        report['checks'][w+'_two_cycles']=len(rotations)>=2
        report['checks'][w+'_midpoint_issuance']=bool(rotations) and all(abs(x['issuance_offset_seconds'])<=30 for x in rotations)
        report['checks'][w+'_delivery_within_120_seconds']=bool(rotations) and all(-5<=x['delivery_offset_seconds']<=120 for x in rotations)
        report['checks'][w+'_replicas_converged']=converged(end,w,expected[w])
        owner='exp1' if w=='cm' else 'exp2'
        report['checks'][w+'_one_issuance_per_cycle']=bool(rotations) and rotations[-1]['counters'][owner]['issuance']-start['mock']['counters'][owner]['issuance']==len(rotations)
    report['checks']['agent_no_tls_secret']=not any('tls.crt' in s.get('fields',[]) or 'tls.key' in s.get('fields',[]) or s['type']=='kubernetes.io/tls' for s in end['experiments']['agent']['secrets'])
    report['checks']['cm_tls_secret_present']=any(s['type']=='kubernetes.io/tls' for s in end['experiments']['cm']['secrets'])
    report['passed']=all(report['checks'].values());report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    (ART/'renewal-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':report['passed'],'checks':report['checks']},indent=2))
    return 0 if report['passed'] else 1
if __name__=='__main__':sys.exit(main())
