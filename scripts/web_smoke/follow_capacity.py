#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Bounded eight-session HTTP transport stress, NOT browser six-connection acceptance.

Four hanging turn polls share the park pool with eight synthetic window readers.
Bounds calls run at ten/s total. Native/browser capacity must be tested separately.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from httpraw import LauncherClient, doc, jbody, request
from follow_probe import RECT
from run_gates import write_json
import targets


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',choices=('stub','plugin'),required=True)
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2])
    p.add_argument('--seconds',type=float,default=30)
    p.add_argument('--json',type=Path,required=True)
    a=p.parse_args()
    if not 1<=a.seconds<=60: p.error('seconds must be 1..60')
    bundle=a.repo/'plugin/sidebar'
    t=targets.StubTarget(bundle,fast=False,repo=a.repo) if a.target=='stub' else targets.PluginTarget(bundle,'plugin_factory:make_endpoint',a.repo)
    result={'target':a.target,'kind':'synthetic HTTP only, not native/browser capacity','status':'error',
            'bounds':[],'window_responses':[0]*8,'stop_ms':[],'max_sampled_parked':0,'errors':[]}
    done=threading.Event(); tokens=[]; lids=[]; turns=[]; futures=[]; pool=ThreadPoolExecutor(max_workers=12)
    def post(path,payload,token=None):
        h=[('Origin',t.origin),('Content-Type','application/json')]
        if token:h.append(('X-Sidecar-Session',token))
        return request(t.port,'POST',path,h,jbody(payload),timeout=6)
    def get(path,token):
        return request(t.port,'GET',path,[('Sec-Fetch-Site','same-origin'),('X-Sidecar-Session',token)],timeout=28)
    def reader(i):
        seq=0
        while not done.is_set():
            response=get('/api/window?seq='+str(seq),tokens[i])
            if response.status!=200:
                if not done.is_set():result['errors'].append({'reader':i,'status':response.status,'code':response.code})
                break
            data=response.json();seq=data['window']['seq'];result['window_responses'][i]+=1
            done.wait(max(.01,data['retry_ms']/1000))
    try:
        assert t.token is not None
        c=LauncherClient(t.port,t.token);hello=c.hello()
        if 'window_follow' not in hello.get('caps',[]):
            result['status']='blocked';result['reason']='endpoint has no revision-5 follow capability'
        else:
            for i in range(8):
                r=c.launch(doc(900+i),follow=True)
                if r.status!=201:raise RuntimeError('launch refused '+str(r.status))
                data=r.json();lids.append(data['launch_id'])
                tokens.append(post('/api/session',{'launch':data['url'].split('#launch=')[1]}).json()['session'])
            for i in range(4):
                turn=post('/api/chat',{'message':'[hang]'},tokens[i]).json()['turn'];turns.append(turn)
            time.sleep(.25)
            for i in range(4):
                state=get('/api/state',tokens[i]).json()['turn']
                futures.append(pool.submit(get,'/api/turn?id='+state['id']+'&seq='+str(state['seq'])+'&have=0',tokens[i]))
            futures += [pool.submit(reader,i) for i in range(8)]
            start=time.monotonic(); index=0
            while time.monotonic()-start<a.seconds:
                tick=time.monotonic(); i=index%8
                if c.counter>=47:c=LauncherClient(t.port,t.token);c.hello()
                r=c.call('POST','/launcher/bounds',{'launch_id':lids[i],'seq':index+1,'state':'normal','bounds':dict(RECT,x=900+index%20)})
                result['bounds'].append({'i':i,'status':r.status,'ms':r.elapsed*1000})
                if r.status!=200:result['errors'].append({'bounds_status':r.status,'code':r.code})
                if isinstance(t,targets.StubTarget):
                    with t.stub.cv:result['max_sampled_parked']=max(result['max_sampled_parked'],t.stub.parked)
                index+=1;done.wait(max(0,.1-(time.monotonic()-tick)))
            result['duration_seconds']=time.monotonic()-start
            for i in range(4):
                r=post('/api/turn/stop',{'turn':turns[i]['id']},tokens[i]);result['stop_ms'].append(r.elapsed*1000)
                if r.status not in (200,202):result['errors'].append({'stop_status':r.status})
            result['status']='pass' if not result['errors'] and all(result['window_responses']) and result['max_sampled_parked']<=4 else 'fail'
    except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)
    finally:
        done.set()
        for token in tokens:post('/api/session/close',{},token)
        pool.shutdown(wait=True,cancel_futures=True)
        for f in futures:
            if not f.cancelled():
                try:f.result()
                except Exception as exc:result['errors'].append({'reader_error':type(exc).__name__+': '+str(exc)})
        t.close()
        if result['errors']:result['status']='fail'
        write_json(a.json,result)
    print(json.dumps({'status':result['status'],'bounds':len(result['bounds']),'readers':result['window_responses'],'stop_ms':result['stop_ms']}))
    return 0 if result['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
