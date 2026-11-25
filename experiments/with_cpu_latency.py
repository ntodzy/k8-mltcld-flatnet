#!/usr/bin/env python3
"""Run one command with temporary hypervisor CPU latency QoS; always restore it."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from common import GENERATED, ROOT, save_json
from cpu_latency import requests, close_requests


def execute(command, output, limit=10, lifetime=3600):
    output.mkdir(parents=True,exist_ok=False)
    metadata=dict(command=command,requested_us=limit,lifetime_seconds=lifetime,status='running',
                  started_utc=datetime.now(timezone.utc).isoformat())
    save_json(output/'policy.json',metadata)
    holders=[];process=None
    try:
        holders=requests(limit,output/'requests',lifetime=lifetime)
        if not holders:raise RuntimeError('No hypervisor CPU policy holders were created')
        process=subprocess.Popen(command,start_new_session=True)
        while process.poll() is None:
            if any(h.process.poll() is not None for h in holders):
                raise RuntimeError('CPU policy lease ended before the command; command stopped')
            time.sleep(.5)
        if any(h.process.poll() is not None for h in holders):
            raise RuntimeError('CPU policy lease expired; refusing successful measurement')
        metadata['exit_code']=process.returncode if process.returncode>=0 else 128-process.returncode
        metadata['status']='passed' if process.returncode==0 else 'failed'
    except BaseException as error:
        metadata.update(status='failed',error=str(error),exit_code=130 if isinstance(error,KeyboardInterrupt) else 1)
    finally:
        try:
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGINT)
                try:process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
        except BaseException as error:
            metadata.update(status='failed',exit_code=1,command_cleanup_error=str(error))
        finally:
            try:
                close_requests(holders)
                metadata['restoration_verified']=True if holders else None
            except BaseException as error:
                metadata.update(status='failed',exit_code=1,restoration_error=str(error),restoration_verified=False)
            metadata['finished_utc']=datetime.now(timezone.utc).isoformat()
            save_json(output/'policy.json',metadata)
    return metadata['exit_code']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit-us',type=int,default=10)
    parser.add_argument('--lifetime',type=int,default=3600,help='Maximum lease in seconds (1–7200); expiry stops the command')
    parser.add_argument('--output',type=Path)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command or not 0<=args.limit_us<=1000000 or not 1<=args.lifetime<=7200:
        parser.error('Supply a command after --, a nonnegative limit <=1000000µs and lifetime1–7200s')
    output=(args.output or ROOT/'results'/('cpu-policy-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))).resolve()
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if any((GENERATED/p).exists() for p in ('path-control.json','forwarding-control.json')):
            parser.error('Restore active forwarding/path controls first')
        print('CPU policy evidence: '+str(output),flush=True)
        def interrupted(*_):raise KeyboardInterrupt
        signal.signal(signal.SIGTERM,interrupted)
        return execute(command,output,args.limit_us,args.lifetime)


if __name__=='__main__':raise SystemExit(main())
