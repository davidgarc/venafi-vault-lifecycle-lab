#!/usr/bin/env python3
"""Observe the native no-running-Agents case; no direct issuance calls."""
import datetime,json,pathlib,sys,time
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import lab
sys.path.insert(0,str(ROOT/'experiments/agent'))
from observe import snapshot
from verify import counters,identity,ready,delta
url='http://127.0.0.1:18201/api/state'
report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':{}}
replicas=None
mutated=False
try:
    before=snapshot();replicas=json.loads(lab.k('agent','-n','demo','get','deployment/demo','-o','json',capture=True))['spec']['replicas']
    if not replicas or len(before['pods'])!=replicas or not all(p.get('certificate',{}).get('valid') for p in before['pods']) or len(identity(before))!=1:raise RuntimeError('Expected ready shared Agent identity')
    cert=before['pods'][0]['certificate']
    if cert['midpoint']<time.time():raise RuntimeError('Run after natural rotation, before next midpoint')
    count=counters(url);report.update(before=before,counters_before=count)
    mutated=True
    lab.k('agent','-n','demo','scale','deployment/demo','--replicas=0',capture=True)
    lab.k('agent','-n','demo','wait','--for=delete','pod','-l','app=demo','--timeout=120s',capture=True)
    while time.time()<cert['midpoint']+15:
        print('All Agent pods stopped; waiting for original midpoint.',file=sys.stderr,flush=True)
        time.sleep(min(30,max(1,cert['midpoint']+15-time.time())))
    stopped=counters(url);report['counters_while_stopped']=stopped
    report['checks']['no_background_issuance_without_agents']=stopped.get('exp2',{}).get('issuance',0)==count.get('exp2',{}).get('issuance',0)
    lab.k('agent','-n','demo','scale','deployment/demo','--replicas='+str(replicas),capture=True)
    after=ready(replicas);end=counters(url)
    report.update(after=after,counter_delta=delta(count,end))
    report['checks']['new_shared_identity_on_demand']=len(identity(after))==1 and identity(before)!=identity(after)
    report['checks']['exactly_one_issuance']=report['counter_delta']['exp2']['issuance']==1
    report['checks']['exactly_one_retrieval']=report['counter_delta']['exp2']['retrieval']==1
    report['checks']['all_pairs_match']=all(p.get('certificate_key_match') for p in after['pods'])
except Exception as exc:report['error']=str(exc)
finally:
    if mutated:
        try:
            lab.k('agent','-n','demo','scale','deployment/demo','--replicas='+str(replicas),capture=True)
            ready(replicas)
        except Exception as exc:report['recovery_error']=str(exc)
    report['passed']=bool(report['checks']) and all(report['checks'].values()) and 'error' not in report and 'recovery_error' not in report
    report['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    (ROOT/'artifacts/demand-driven.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'passed':report['passed'],'checks':report['checks'],'error':report.get('error')}));sys.exit(0 if report['passed'] else 1)
