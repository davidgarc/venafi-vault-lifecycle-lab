import concurrent.futures, datetime, json, pathlib, ssl, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CONFIG=json.loads(pathlib.Path('/config/config.json').read_text())
LOCK=threading.Lock()

def fetch(url,token=None,ca=None,data=None,method=None,patch=False):
    headers={}
    if token:headers['Authorization']='Bearer '+token
    if data is not None:headers['Content-Type']='application/merge-patch+json' if patch else 'application/json'
    req=urllib.request.Request(url,headers=headers,data=json.dumps(data).encode() if data is not None else None,method=method)
    ctx=ssl.create_default_context(cadata=ca) if ca else None
    with urllib.request.urlopen(req,context=ctx,timeout=8) as r:
        return json.loads(r.read())
def kube(which,path,data=None,method=None,patch=False):
    c=CONFIG['clusters'][which]
    return fetch(c['api']+path,c['token'],c['ca'],data,method,patch)
def experiment(which):
    out={'experiment':which,'observed_at':time.time()}
    try:
        out['desired_replicas']=kube(which,'/apis/apps/v1/namespaces/demo/deployments/demo')['spec'].get('replicas',1)
        pods=kube(which,'/api/v1/namespaces/demo/pods?labelSelector=app%3Ddemo')['items']
        out['pods']=[]
        for p in pods:
            if p['metadata'].get('deletionTimestamp'):continue
            item={'name':p['metadata']['name'],'uid':p['metadata']['uid'],'phase':p['status']['phase'],'restarts':sum(c['restartCount'] for c in p['status'].get('containerStatuses',[]))}
            try:item['certificate']=kube(which,'/api/v1/namespaces/demo/pods/'+item['name']+':8080/proxy/cgi-bin/cert')
            except Exception:item['error']='Certificate endpoint not ready'
            out['pods'].append(item)
        secrets=kube(which,'/api/v1/namespaces/demo/secrets')['items']
        out['secrets']=[{'name':s['metadata']['name'],'type':s.get('type'),'fields':list(s.get('data',{}))} for s in secrets]
        if which=='cm':
            certs=kube(which,'/apis/cert-manager.io/v1/namespaces/demo/certificates')['items']
            out['certificates']=[{'name':c['metadata']['name'],'status':c.get('status',{})} for c in certs]
    except Exception as e:out['error']=str(e)
    return out

def snapshot():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures={w:ex.submit(experiment,w) for w in CONFIG['clusters']}
        try:mock=fetch(CONFIG['mock']+'/api/state')
        except Exception as e:mock={'error':str(e)}
        return {'observed_at':time.time(),'experiments':{w:f.result() for w,f in futures.items()},'mock':mock}
class Handler(BaseHTTPRequestHandler):
    def send(self,status,data,ctype='application/json'):
        raw=data if isinstance(data,bytes) else json.dumps(data).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/':return self.send(200,pathlib.Path('/app/index.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/api/state':return self.send(200,snapshot())
        self.send(404,{'error':'Not found'})
    def do_POST(self):
        # Only same-origin requests from the loopback UI with an explicit custom header.
        host=self.headers.get('Host','')
        if host not in ('127.0.0.1:18080','localhost:18080') or self.headers.get('Origin')!='http://'+host or self.headers.get('X-Lab-Action')!='1':
            return self.send(403,{'error':'Same-origin lab control required'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<1024:raise ValueError('Invalid body')
            body=json.loads(self.rfile.read(size))
            which=body.get('experiment');action=body.get('action')
            if self.path!='/api/action' or which not in (*CONFIG['clusters'],'mock'):raise ValueError('Unknown experiment')
            with LOCK:
                if which=='mock' and action in ('pause','resume'):
                    c=CONFIG['control']
                    fetch(c['api']+'/apis/apps/v1/namespaces/lab/deployments/mock-tpp',c['token'],c['ca'],{'spec':{'replicas':0 if action=='pause' else 1}},'PATCH',True)
                elif which=='mock':raise ValueError('Unknown mock control')
                elif action=='restart':
                    kube(which,'/apis/apps/v1/namespaces/demo/deployments/demo',{'spec':{'template':{'metadata':{'annotations':{'lab/restartedAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}}}}},'PATCH',True)
                elif action=='scale':
                    count=body.get('replicas')
                    if type(count) is not int or count not in (0,1,3):raise ValueError('Allowed replicas: 0, 1, 3')
                    kube(which,'/apis/apps/v1/namespaces/demo/deployments/demo',{'spec':{'replicas':count}},'PATCH',True)
                else:raise ValueError('Unknown action')
            self.send(202,{'accepted':True,'action':action,'experiment':which})
        except Exception as e:self.send(400,{'error':str(e)})
    def log_message(self,*args):pass
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
