#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Real-page follow policy checks. Headless policy evidence, never native geometry evidence."""
import argparse
import json
from pathlib import Path
import time
import browser_smoke
from cdp import Chrome
from httpraw import LauncherClient, doc
from follow_probe import RECT
import targets
from run_gates import write_json

PRELOAD = r"""
(() => {
  const r = window.__followEvidence = {polls: [], placements: []};
  const originalFetch = window.fetch;
  window.fetch = function(input, options) {
    const path = typeof input === 'string' ? input : input.url;
    if (/\/api\/window\?/.test(path)) r.polls.push({t: performance.now(), query: path.split('?')[1]});
    return originalFetch.call(this, input, options);
  };
  for (const name of ['resizeTo','moveTo']) {
    const original = window[name];
    window[name] = function(...args) {r.placements.push({name,args,t:performance.now()});return original.apply(this,args);};
  }
  localStorage.removeItem('sidecar.follow');
})();
"""


def run(target, chrome, checks=None):
    ctx = browser_smoke.BCtx(target, chrome, None, 'follow')
    if checks is not None: ctx.checks = checks
    try:
        client = LauncherClient(target.port,target.token)
        hello = client.hello()
        ctx.check('follow capability available', 'window_follow' in hello.get('caps',[]))
        launched = client.launch(doc(701),follow=True)
        ctx.check('signed follow launch',launched.status == 201 and launched.json().get('follow') is True,
                  observed={'status':launched.status,'code':launched.code})
        if launched.status != 201: return ctx.checks
        data = launched.json()
        # Install instrumentation before the first page script, preserving the native APIs.
        from cdp import Page
        page = Page(chrome,ctx.bootstrap(data['url']).as_uri(),preload=browser_smoke.PRELOAD+'\n'+PRELOAD)
        ctx.pages.append(page)
        ctx.check('page connected',ctx.wait_connected(page))
        page.eval("document.getElementById('settings-button').click()")
        ctx.check('single follow ids outside transcript',page.eval("['follow-toggle','follow-status','reattach-button'].every(id => document.querySelectorAll('#'+id).length===1 && !document.getElementById('messages').contains(document.getElementById(id)))"))
        ctx.check('default OFF checkbox',page.eval("!!document.getElementById('follow-toggle') && !document.getElementById('follow-toggle').checked"))
        time.sleep(3.3)
        ctx.check('default OFF no window polls',page.eval('__followEvidence.polls.length') == 0)
        # Initial launch placement is allowed. Count later follow placement only after opt-in.
        page.eval("document.getElementById('follow-toggle').click();document.getElementById('save-settings-button').click()")
        ctx.check('explicit opt-in persisted',page.eval("localStorage.getItem('sidecar.follow') === 'on'"))
        ctx.check('opt-in starts polling',page.wait_for('__followEvidence.polls.length > 0',5))
        sent = client.call('POST','/launcher/bounds',{'launch_id':data['launch_id'],'seq':1,'state':'normal','bounds':RECT})
        ctx.check('normal snapshot accepted',sent.status == 200)
        ctx.check('resize precedes move',page.wait_for("__followEvidence.placements.slice(-2).map(x=>x.name).join(',') === 'resizeTo,moveTo'",5))
        time.sleep(.6)
        baseline = page.eval('__followEvidence.placements.length')
        client.call('POST','/launcher/bounds',{'launch_id':data['launch_id'],'seq':2,'state':'minimized','bounds':None})
        ctx.check('minimized status',page.wait_for("document.getElementById('follow-status').textContent.includes('minimised')",4))
        ctx.check('minimized no placement',page.eval('__followEvidence.placements.length') == baseline)
        client.call('POST','/launcher/bounds',{'launch_id':data['launch_id'],'seq':3,'state':'normal','bounds':dict(RECT,x=920)})
        ctx.check('restore resumes placement',page.wait_for('__followEvidence.placements.length > '+str(baseline),5))
        ctx.check('Reattach is a button',page.eval("document.getElementById('reattach-button').tagName === 'BUTTON'"))
        ctx.check('fragment cleared',page.eval("location.hash === ''"))
        # No snapshot token or launch URL is recorded in the result.
        ctx.check('follow trace captured',True,observed=page.eval('__followEvidence'))
        return ctx.checks
    finally:
        ctx.close()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target',choices=('stub','plugin','external'),required=True)
    ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2])
    ap.add_argument('--bundle',type=Path)
    ap.add_argument('--port',type=int)
    ap.add_argument('--token-file',type=Path)
    ap.add_argument('--json',type=Path,required=True)
    a=ap.parse_args(); bundle=a.bundle or a.repo/'plugin/sidebar'
    if a.target=='external':
        if not a.port or not a.token_file: ap.error('external needs --port and --token-file')
        target=targets.ExternalTarget(a.port,a.token_file)
    elif a.target=='plugin': target=targets.PluginTarget(bundle,'plugin_factory:make_endpoint',a.repo)
    else: target=targets.StubTarget(bundle,fast=False,repo=a.repo)
    chrome=None; result={'target':a.target,'kind':'headless-policy-only','checks':[],'status':'error'}
    try:
        chrome=Chrome()
        run(target,chrome,result['checks'])
        result['status']='pass' if all(x['ok'] for x in result['checks']) else 'fail'
    except Exception as exc: result['error']=type(exc).__name__+': '+str(exc)
    finally:
        if chrome: chrome.close()
        target.close()
    write_json(a.json,result); print(json.dumps({'target':a.target,'status':result['status'],'checks':len(result['checks'])}))
    return 0 if result['status']=='pass' else 1


if __name__=='__main__': raise SystemExit(main())
