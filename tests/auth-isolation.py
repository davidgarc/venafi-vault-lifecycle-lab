#!/usr/bin/env python3
"""Negative authentication checks; JWTs stay in memory and never enter evidence."""
import json, pathlib, sys, urllib.error
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import lab
results={}
for source,target,sa in [('cm','agent','vault-issuer'),('agent','cm','demo'),('agent','agent','default')]:
    audience='https://kubernetes.default.svc.cluster.local' if target=='agent' else 'vault://demo/vault'
    jwt=lab.k(source,'-n','demo','create','token',sa,'--audience='+audience,capture=True)
    try:
        lab.vault('auth/kubernetes-'+target+'/login',{'role':'demo','jwt':jwt})
        results[source+'-'+sa+'-to-'+target]={'rejected':False}
    except urllib.error.HTTPError as e:
        results[source+'-'+sa+'-to-'+target]={'rejected':e.code in (400,403),'http_status':e.code}
(ROOT/'artifacts/auth-isolation.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results));sys.exit(0 if all(x['rejected'] for x in results.values()) else 1)
