"""Temporary actual-Q web startup regression; no Candidate execution authority."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

REPO='yanyuhanyue/AniMemo'
RUN=34682626813
SHA='0817b5fcf1f50971fb896a8e429f230ee7cd1694'
TREE='0a8da1e114f182730dda4a81e22f7912c1b4d81a'
FINAL_ID=10295096835
FINAL_DIGEST='sha256:b1fe803dd640f2d8dd5c02a907a7afa558a6efb343472f0baf236d93dddc53eb'

def command(argv, *, cwd=None, environment=None, codes=(0,), timeout=180, merge_stderr=False):
    process=subprocess.Popen(argv,cwd=cwd,env=environment,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL)
    timer=threading.Timer(timeout,process.kill); timer.daemon=True; timer.start()
    output=bytearray()
    try:
        while chunk:=process.stdout.read1(65536):
            if len(output)+len(chunk)>8*1024*1024: raise RuntimeError('DEVELOPMENT_OUTPUT_LIMIT')
            output.extend(chunk)
        if process.wait(timeout=10) not in codes: raise RuntimeError('DEVELOPMENT_COMMAND_FAILED')
        return bytes(output)
    finally:
        timer.cancel()
        if process.poll() is None: process.kill(); process.wait(timeout=10)
        process.stdout.close(); output[:]=bytes(len(output))

def save(path,value):
    with path.open('x',encoding='utf-8') as f: json.dump(value,f,indent=2)

def download(root):
    root.mkdir()
    def api(endpoint): return json.loads(command(['gh','api',endpoint]))
    run=api(f'repos/{REPO}/actions/runs/{RUN}')
    jobs=api(f'repos/{REPO}/actions/runs/{RUN}/attempts/1/jobs?per_page=100')
    artifacts=api(f'repos/{REPO}/actions/runs/{RUN}/artifacts?per_page=100')
    assert run['head_sha']==SHA and run['run_attempt']==1 and run['status']=='completed' and run['conclusion']=='success'
    for key,value in [('jobs',jobs),('artifacts',artifacts)]:
        assert len(value[key])==value['total_count'] and len({x['id'] for x in value[key]})==value['total_count']
    final=next(x for x in artifacts['artifacts'] if x['id']==FINAL_ID)
    assert final['digest']==FINAL_DIGEST and not final['expired']
    assert final['workflow_run']['id']==RUN and final['workflow_run']['head_sha']==SHA
    for name,value in [('run.json',run),('jobs.json',jobs),('artifacts.json',artifacts)]: save(root/name,value)
    archive=root/'evidence.zip'
    with archive.open('xb') as output:
        subprocess.run(['gh','api',f'repos/{REPO}/actions/artifacts/{FINAL_ID}/zip'],
            stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL,check=True,timeout=600)
    with archive.open('rb') as f: digest='sha256:'+hashlib.file_digest(f,'sha256').hexdigest()
    assert archive.stat().st_size==final['size_in_bytes'] and digest==FINAL_DIGEST
    print('ACTUAL_Q_ARCHIVE_API_AND_BYTES_VERIFIED')

