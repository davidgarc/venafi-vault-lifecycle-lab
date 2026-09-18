#!/usr/bin/env python3
"""Explicitly scoped local lab lifecycle. No global kubeconfig writes."""
import base64, concurrent.futures, json, os, pathlib, shutil, subprocess, sys, time, urllib.request, urllib.error
ROOT=pathlib.Path(__file__).resolve().parents[1]
STATE=ROOT/'.state'
VERS=json.loads((ROOT/'infra/versions.json').read_text())
NAMES={x:'venafi-lab-'+x for x in ('vault','cm','agent')}
PORTS={'vault':[(30200,18200),(30201,18201)],'cm':[(30080,18081)],'agent':[(30080,18082)]}
os.umask(0o077)
STATE.mkdir(exist_ok=True)
STATE.chmod(0o700)
os.environ['KUBECONFIG']=str(STATE/'kubeconfig')

def run(*args, capture=False, data=None, env=None):
    p=subprocess.run(list(map(str,args)),input=data,text=True,stdout=subprocess.PIPE if capture else None,check=True,env=env)
    return p.stdout.strip() if capture else ''
def k(which,*args,capture=False,data=None):
    assert which in NAMES
    return run('kubectl','--context','kind-'+NAMES[which],*args,capture=capture,data=data)
def apply(which,*objects):
    k(which,'apply','-f','-',data=json.dumps({'apiVersion':'v1','kind':'List','items':list(objects)}))
def obj(kind,name,spec=None,namespace='lab',api='v1',**extra):
    o={'apiVersion':api,'kind':kind,'metadata':{'name':name,'namespace':namespace}}
    if spec is not None:o['spec']=spec
    o.update(extra)
    return o
def node_ip(which):
    return run('docker','inspect',NAMES[which]+'-control-plane','--format','{{with index .NetworkSettings.Networks "kind"}}{{.IPAddress}}{{end}}',capture=True)
def request(url,data=None,token=None,method=None):
    headers={'Content-Type':'application/json'}
    if token:headers['X-Vault-Token']=token
    req=urllib.request.Request(url,data=None if data is None else json.dumps(data).encode(),headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=30) as r:
        body=r.read()
        return json.loads(body) if body else {}
def vault(path,data=None,method=None):
    token=json.loads((STATE/'vault-init.json').read_text())['root_token'] if (STATE/'vault-init.json').exists() else None
    return request('http://127.0.0.1:18200/v1/'+path,data,token,method)
def wait_http(url,timeout=180):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            with urllib.request.urlopen(url,timeout=3) as r:return r.read()
        except Exception:time.sleep(2)
    raise RuntimeError('Timed out: '+url)
def doctor():
    for cmd in ('docker','kind','kubectl','helm','git'):
        if not shutil.which(cmd):raise RuntimeError('Missing '+cmd)
    run('docker','info','--format','Docker {{.ServerVersion}}, {{.Architecture}}, CPUs={{.NCPU}}, memory={{.MemTotal}}')
    run('kind','get','clusters')
    print('Lab owns only venafi-lab-{vault,cm,agent}, venafi-lab-ui, and .state. HTTP ports: 18080/18081/18082/18200/18201.')
def build():
    for name,path in [('vault','infra/Dockerfile.vault'),('mock-tpp','mock-tpp/Dockerfile'),('demo','demo-app/Dockerfile'),('ui','demo-ui/Dockerfile')]:
        run('docker','build','-f',ROOT/path,'-t','venafi-lab/'+name+':local',ROOT if name=='vault' else ROOT/pathlib.Path(path).parent)
def clusters(targets):
    existing=run('kind','get','clusters',capture=True).splitlines()
    owned=set(json.loads((STATE/'owned.json').read_text())) if (STATE/'owned.json').exists() else set()
    for which in targets:
        name=NAMES[which]
        if name in existing:
            if name not in owned:raise RuntimeError('Refusing to adopt existing unowned cluster '+name)
            saved=STATE/(which+'-container-id')
            live=run('docker','inspect',name+'-control-plane','--format','{{.Id}}',capture=True)
            if not saved.exists() or saved.read_text().strip()!=live:raise RuntimeError('Cluster container identity changed: '+name)
            run('kind','export','kubeconfig','--name',name,'--kubeconfig',STATE/'kubeconfig')
            continue
        cfg={'kind':'Cluster','apiVersion':'kind.x-k8s.io/v1alpha4','nodes':[{'role':'control-plane','extraPortMappings':[{'containerPort':c,'hostPort':h,'listenAddress':'127.0.0.1'} for c,h in PORTS[which]]}]}
        config=STATE/(which+'-kind.json');config.write_text(json.dumps(cfg))
        # Reserve ownership before creation so partial creation can be safely cleaned.
        owned.add(name);(STATE/'owned.json').write_text(json.dumps(sorted(owned)))
        try:
            run('kind','create','cluster','--name',name,'--image',VERS['kind_node'],'--config',config,'--kubeconfig',STATE/'kubeconfig','--wait','180s')
        finally:
            live=run('docker','ps','-a','--filter','name=^/'+name+'-control-plane$','--format','{{.ID}}',capture=True)
            if live:(STATE/(which+'-container-id')).write_text(run('docker','inspect',name+'-control-plane','--format','{{.Id}}',capture=True))

def deployment(name,image,ports,volumes=None,mounts=None,env=None,args=None,command=None):
    container={'name':name,'image':image,'imagePullPolicy':'Never','ports':[{'containerPort':p} for p in ports],'resources':{'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'memory':'768Mi'}}}
    if mounts:container['volumeMounts']=mounts
    if env:container['env']=[{'name':n,'value':v} for n,v in env.items()]
    if args:container['args']=args
    if command:container['command']=command
    return obj('Deployment',name,{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':name}},'template':{'metadata':{'labels':{'app':name}},'spec':{'containers':[container],'volumes':volumes or [],'securityContext':{'runAsUser':0}}}},api='apps/v1')
def service(name,ports):
    return obj('Service',name,{'type':'NodePort','selector':{'app':name},'ports':[{'name':n,'port':p,'targetPort':p,'nodePort':np} for n,p,np in ports]})
def lifetime():
    saved=json.loads((STATE/'runtime.json').read_text()).get('lifetime_seconds') if (STATE/'runtime.json').exists() else None
    seconds=int(os.environ.get('CERT_LIFETIME_SECONDS',saved or 720))
    if seconds<720:raise ValueError('Use >=720 seconds to leave margin above cert-manager minimum renewal lead time')
    if saved is not None and seconds!=saved:raise ValueError('Changing lifetime requires a fresh lab: make down first')
    return seconds

def shared():
    seconds=lifetime()
    for image in ('vault','mock-tpp'):run('kind','load','docker-image','venafi-lab/'+image+':local','--name',NAMES['vault'])
    apply('vault',obj('Namespace','lab',namespace='lab'))
    apply('vault',deployment('mock-tpp','venafi-lab/mock-tpp:local',[8443,8080],volumes=[{'name':'data','hostPath':{'path':'/var/local/venafi-lab/mock','type':'DirectoryOrCreate'}}],mounts=[{'name':'data','mountPath':'/data'}],env={'CERT_LIFETIME_SECONDS':str(seconds)}),service('mock-tpp',[('https',8443,30202),('status',8080,30201)]))
    k('vault','-n','lab','rollout','status','deployment/mock-tpp','--timeout=180s')
    ca=wait_http('http://127.0.0.1:18201/ca.pem').decode()
    cfg='ui = true\ndisable_mlock = true\nplugin_directory = "/vault/plugins"\nstorage "file" { path = "/vault/file" }\nlistener "tcp" { address = "0.0.0.0:8200" tls_disable = true }\napi_addr = "http://vault:8200"\n'
    apply('vault',obj('ConfigMap','vault-config',data={'vault.hcl':cfg,'ca.pem':ca}))
    apply('vault',deployment('vault','venafi-lab/vault:local',[8200],volumes=[{'name':'data','hostPath':{'path':'/var/local/venafi-lab/vault','type':'DirectoryOrCreate'}},{'name':'config','configMap':{'name':'vault-config'}}],mounts=[{'name':'data','mountPath':'/vault/file'},{'name':'config','mountPath':'/vault/lab-config','readOnly':True}],env={'VAULT_ADDR':'http://127.0.0.1:8200','SKIP_SETCAP':'true'},args=['server','-config=/vault/lab-config/vault.hcl']),service('vault',[('http',8200,30200)]))
    k('vault','-n','lab','rollout','status','deployment/vault','--timeout=180s')
    wait_http('http://127.0.0.1:18200/v1/sys/init')
    if not vault('sys/init')['initialized']:
        result=vault('sys/init',{'secret_shares':1,'secret_threshold':1})
        (STATE/'vault-init.json').write_text(json.dumps(result))
    if vault('sys/seal-status')['sealed']:
        vault('sys/unseal',{'key':json.loads((STATE/'vault-init.json').read_text())['keys_base64'][0]})
    checksum=k('vault','-n','lab','exec','deployment/vault','--','sha256sum','/vault/plugins/venafi-pki-backend',capture=True).split()[0]
    vault('sys/plugins/catalog/secret/venafi-pki-backend',{'sha256':checksum,'command':'venafi-pki-backend'})
    mounts=vault('sys/mounts')
    for which in ('cm','agent'):
        mount='venafi-'+which
        if mount+'/' not in mounts:vault('sys/mounts/'+mount,{'type':'venafi-pki-backend'})
        vault(mount+'/venafi/mock',{'url':'https://mock-tpp:8443','zone':'\\VED\\Policy\\'+which,'access_token':'lab-token-'+which,'trust_bundle_file':'/vault/lab-config/ca.pem'})
        role={'venafi_secret':'mock','ttl':'1h','max_ttl':'1h','generate_lease':False,'store_by':'hash' if which=='agent' else 'serial','store_pkey':which=='agent','min_cert_time_left':str(int(str(seconds))//2)+'s','no_store':False}
        vault(mount+'/roles/demo',role)
    (STATE/'runtime.json').write_text(json.dumps({'vault_addr':'http://'+node_ip('vault')+':30200','lifetime_seconds':int(str(seconds))},indent=2))

def auth(which):
    apply(which,obj('Namespace','demo',namespace='demo'),obj('ServiceAccount','vault-reviewer',namespace='demo'),obj('ClusterRoleBinding','venafi-lab-reviewer',namespace='demo',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'system:auth-delegator'},subjects=[{'kind':'ServiceAccount','name':'vault-reviewer','namespace':'demo'}]))
    ca=k(which,'config','view','--raw','--minify','-o','jsonpath={.clusters[0].cluster.certificate-authority-data}',capture=True)
    reviewer=k(which,'-n','demo','create','token','vault-reviewer','--duration=24h',capture=True)
    mount='kubernetes-'+which
    if mount+'/' not in vault('sys/auth'):vault('sys/auth/'+mount,{'type':'kubernetes'})
    vault('auth/'+mount+'/config',{'kubernetes_host':'https://'+node_ip(which)+':6443','kubernetes_ca_cert':base64.b64decode(ca).decode(),'token_reviewer_jwt':reviewer,'disable_local_ca_jwt':True})
    path='venafi-'+which+('/sign/demo' if which=='cm' else '/issue/demo')
    vault('sys/policies/acl/demo-'+which,{'policy':'path "'+path+'" { capabilities = ["update"] }'})
    vault('auth/'+mount+'/role/demo',{'bound_service_account_names':['vault-issuer' if which=='cm' else 'demo'],'bound_service_account_namespaces':['demo'],'policies':['demo-'+which],'ttl':'1h','audience':'vault://demo/vault' if which=='cm' else 'https://kubernetes.default.svc.cluster.local'})

def install(which):
    run('kind','load','docker-image','venafi-lab/demo:local','--name',NAMES[which])
    auth(which)
    env=os.environ.copy();env['VAULT_ADDR']='http://'+node_ip('vault')+':30200'
    run('bash',ROOT/'experiments'/('cert-manager' if which=='cm' else 'agent')/'install.sh',env=env)

def dashboard():
    config={}
    existing=run('kind','get','clusters',capture=True).splitlines()
    for which in ('cm','agent'):
        if NAMES[which] not in existing:continue
        apply(which,obj('ServiceAccount','demo-ui',namespace='demo'),obj('Role','demo-ui',namespace='demo',api='rbac.authorization.k8s.io/v1',rules=[{'apiGroups':[''],'resources':['pods'],'verbs':['get','list','delete']},{'apiGroups':[''],'resources':['pods/proxy'],'verbs':['get']},{'apiGroups':[''],'resources':['secrets'],'verbs':['list']},{'apiGroups':['apps'],'resources':['deployments'],'resourceNames':['demo'],'verbs':['get','patch']},{'apiGroups':['cert-manager.io'],'resources':['certificates','issuers'],'verbs':['get','list']}]),obj('RoleBinding','demo-ui',namespace='demo',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'demo-ui'},subjects=[{'kind':'ServiceAccount','name':'demo-ui','namespace':'demo'}]))
        ca=k(which,'config','view','--raw','--minify','-o','jsonpath={.clusters[0].cluster.certificate-authority-data}',capture=True)
        config[which]={'api':'https://'+node_ip(which)+':6443','ca':base64.b64decode(ca).decode(),'token':k(which,'-n','demo','create','token','demo-ui','--duration=24h',capture=True),'app':'http://'+node_ip(which)+':30080'}
    apply('vault',obj('ServiceAccount','demo-ui'),obj('Role','demo-ui',api='rbac.authorization.k8s.io/v1',rules=[{'apiGroups':['apps'],'resources':['deployments'],'resourceNames':['mock-tpp'],'verbs':['get','patch']}]),obj('RoleBinding','demo-ui',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'demo-ui'},subjects=[{'kind':'ServiceAccount','name':'demo-ui','namespace':'lab'}]))
    ca=k('vault','config','view','--raw','--minify','-o','jsonpath={.clusters[0].cluster.certificate-authority-data}',capture=True)
    control={'api':'https://'+node_ip('vault')+':6443','ca':base64.b64decode(ca).decode(),'token':k('vault','-n','lab','create','token','demo-ui','--duration=24h',capture=True)}
    (STATE/'ui-config.json').write_text(json.dumps({'clusters':config,'control':control,'mock':'http://'+node_ip('vault')+':30201'}))
    prior=run('docker','ps','-a','--filter','name=^/venafi-lab-ui$','--format','{{.ID}}',capture=True)
    if prior:
        label=run('docker','inspect','venafi-lab-ui','--format','{{index .Config.Labels "venafi-lab.owner"}}',capture=True)
        if label!=str(ROOT):raise RuntimeError('UI container exists but is not owned by this checkout')
        run('docker','rm','-f','venafi-lab-ui')
    run('docker','run','-d','--name','venafi-lab-ui','--label','venafi-lab.owner='+str(ROOT),'--network','kind','-p','127.0.0.1:18080:8080','-v',str(STATE/'ui-config.json')+':/config/config.json:ro','venafi-lab/ui:local')
    wait_http('http://127.0.0.1:18080/')
    print('Demo: http://127.0.0.1:18080 | cert-manager :18081 | Agent :18082 | Vault :18200')

def down(targets):
    owned=set(json.loads((STATE/'owned.json').read_text())) if (STATE/'owned.json').exists() else set()
    if 'vault' in targets:
        prior=run('docker','ps','-a','--filter','name=^/venafi-lab-ui$','--format','{{.ID}}',capture=True)
        if prior:
            label=run('docker','inspect','venafi-lab-ui','--format','{{index .Config.Labels "venafi-lab.owner"}}',capture=True)
            if label!=str(ROOT):raise RuntimeError('Unowned UI container; refusing removal')
            run('docker','rm','-f','venafi-lab-ui')
    for which in targets:
        name=NAMES[which]
        if name not in owned:continue
        live=run('docker','ps','-a','--filter','name=^/'+name+'-control-plane$','--format','{{.ID}}',capture=True)
        saved=STATE/(which+'-container-id')
        if live and (not saved.exists() or not saved.read_text().strip().startswith(live)):
            raise RuntimeError('Cluster container identity changed; refusing to delete '+name)
        run('kind','delete','cluster','--name',name,'--kubeconfig',STATE/'kubeconfig')
        owned.remove(name)
        (STATE/'owned.json').write_text(json.dumps(sorted(owned)))
    if 'vault' in targets:
        # Node-local Vault/mock storage is deleted with kind; remove matching bootstrap credentials.
        for file in ('vault-init.json','ui-config.json','runtime.json'):(STATE/file).unlink(missing_ok=True)
    if not owned:
        for file in STATE.iterdir():
            if file.is_file():file.unlink()
    print('Removed requested lab resources. Downloaded/build images retained for fast next startup.')
def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'doctor'
    os.chdir(ROOT)
    if action=='doctor':doctor()
    elif action=='build':build()
    elif action in ('up','up-cm','up-agent'):
        doctor();build()
        targets=['cm','agent'] if action=='up' else [action[3:]]
        clusters(['vault']+targets);shared()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(install,targets))
        dashboard()
    elif action=='demo':dashboard()
    elif action in ('down','reset'):down(['cm','agent','vault'])
    elif action in ('down-cm','down-agent'):down([action[5:]])
    else:raise SystemExit('Unknown action '+action)
if __name__=='__main__':main()
