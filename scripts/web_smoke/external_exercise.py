#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Exercise ExternalTarget against an owned real plugin, never a live endpoint."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import browser_smoke
import probes
import targets
from run_gates import write_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2])
    p.add_argument('--json',type=Path,required=True)
    a=p.parse_args()
    owners=[]
    try:
        with tempfile.TemporaryDirectory(prefix='external-target-') as folder:
            def make():
                # Each scenario expects a fresh endpoint; external targets themselves own nothing.
                owner=targets.PluginTarget(a.repo/'plugin/sidebar','plugin_factory:make_endpoint',a.repo)
                owners.append(owner)
                token=Path(folder)/('token-'+str(len(owners)))
                fd=os.open(token,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                assert owner.token is not None
                with os.fdopen(fd,'w') as f: f.write(owner.token)
                return targets.ExternalTarget(owner.port,token)
            results=[probes.run_probe(probe,make,False) for probe in probes.PROBES
                     if probe.id in ('g1.host.variants','g1.origin.post','g1.roles','g1.static')]
            results+=browser_smoke.run_scenarios(make,evidence=None,tag='external-real-plugin',
                                                 only=['g3.settings','g5.ax_tree'])
            result={'backing':'real plugin + FakeRunsAPI','target':'ExternalTarget',
                    'results':results,'status':'pass' if all(r['status'] in ('pass','info') for r in results) else 'fail'}
            write_json(a.json,result)
            print(json.dumps({'status':result['status'],'results':len(results)}))
            return 0 if result['status']=='pass' else 1
    finally:
        for owner in owners: owner.close()


if __name__=='__main__': raise SystemExit(main())
