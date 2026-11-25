"""Flow-scoped host bridge bypass sessions for the ordinary Cilium CLI runner."""
import base64
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from common import GENERATED, ROOT, remote, save_json, ssh_command

MARKER = GENERATED/'forwarding-control.json'


def compile_object(directory):
    source=ROOT/'experiments/prototypes/host_bridge_redirect.bpf.c'
    output=directory/'host-redirect.o'
    command=['clang','-target','bpfel','-O2','-g','-Wall','-Werror',
             '-Wno-unused-command-line-argument','-c',str(source),'-o',str(output)]
    result=subprocess.run(command,capture_output=True,text=True)
    save_json(directory/'compile.json',dict(argv=command,exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr))
    if result.returncode:raise RuntimeError(result.stderr)
    (directory/source.name).write_bytes(source.read_bytes())
    return output.read_bytes()


def endpoint_ips(payload, client, server):
    result={}
    for name in (client,server):
        candidates=[p for p in payload['items'] if p['spec'].get('nodeName')==name
                    and not p['spec'].get('hostNetwork',False)
                    and p['status'].get('phase')=='Running'
                    and any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[]))]
        if len(candidates)!=1:return None
        result[name]=str(ipaddress.IPv4Address(candidates[0]['status']['podIP']))
    return result


class Agent:
    def __init__(self,target,spec,directory):
        self.target,self.records,self.ready=target,[],threading.Event()
        directory.mkdir()
        self.stderr=(directory/'stderr.log').open('w')
        source=(ROOT/'experiments/prototypes/host_bypass_agent.py').read_text()
        (directory/'agent.py').write_text(source)
        self.process=subprocess.Popen(ssh_command(GENERATED/'ssh_config',target,
              ['sudo','-n','python3','-u','-c',source]),stdin=subprocess.PIPE,
              stdout=subprocess.PIPE,stderr=self.stderr,text=True)
        def read():
            try:
                with (directory/'session.jsonl').open('w') as out:
                    for line in self.process.stdout:
                        out.write(line);out.flush()
                        record=json.loads(line);self.records.append(record)
                        if record['kind'] in ('ready','error'):self.ready.set()
            finally:self.ready.set()
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        self.process.stdin.write(json.dumps(spec)+'\n');self.process.stdin.flush()

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.close()  # EOF releases the scoped agent.
        try:self.process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            self.process.terminate();self.process.wait(timeout=25)
        self.reader.join(timeout=5)
        self.process.stdout.close();self.stderr.close()
        restored=next((r for r in self.records if r['kind']=='restored'),None)
        if restored is None or restored['errors']:
            raise RuntimeError('Host filter cleanup was not verified on '+self.target)


class HostBypass:
    def __init__(self,args,hosts,controller,namespace,output):
        self.args,self.hosts,self.controller,self.namespace=args,hosts,controller,namespace
        if not output.resolve().is_relative_to((ROOT/'results').resolve()):
            raise ValueError('Host BPF output must be under results/ for ownership-checked recovery')
        self.directory=output/'host-bpf';self.directory.mkdir()
        self.agents=[]
        self.identities=None
        self.object=compile_object(self.directory)
        # Exclusive marker prevents another standalone runner/setup proceeding.
        self.state={'mode':args.mode,'pid':os.getpid(),'status':'preparing','targets':{},
                    'directory':str(self.directory.resolve())}
        with MARKER.open('x') as out:json.dump(self.state,out)

    def attach(self,process):
        deadline=time.monotonic()+180
        ips=None
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Cilium perf exited before BPF attachment')
            try:
                payload=json.loads(remote(GENERATED/'ssh_config',self.controller,
                    ['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf','-n',self.namespace,'get','pods','-o','json']))
                ips=endpoint_ips(payload,self.args.client,self.args.server)
                if ips:break
            except subprocess.CalledProcessError:pass
            time.sleep(2)
        if not ips:raise RuntimeError('Benchmark endpoints did not become Ready')
        started=time.monotonic()
        save_json(self.directory/'endpoints.json',payload)
        self.identities={p['metadata']['uid'] for p in payload['items']
                         if p['spec'].get('nodeName') in ips and not p['spec'].get('hostNetwork',False)}
        config=json.loads((ROOT/'infrastructure/config.json').read_text())
        try:
            for local,peer in [(self.args.client,self.args.server),(self.args.server,self.args.client)]:
                host=next(h for h in config['hypervisors'] if h['name']==self.hosts[local]['hypervisor'])
                vm=next(v for v in host['vms'] if v['name']==local)
                spec={'domain':local,'mac':vm['mac_address'],'local_ip':ips[local],
                      'remote_ip':ips[peer],'bypass':self.args.mode=='host-vxlan-bpf-1450',
                      'object':base64.b64encode(self.object).decode(),
                      'lifetime_seconds':self.args.duration*self.args.samples*6+900}
                agent=Agent('cs740-'+host['name']+'-jump',spec,self.directory/host['name'])
                self.agents.append(agent)
            for agent in self.agents:
                if not agent.ready.wait(max(0,35-(time.monotonic()-started))):
                    raise RuntimeError('Host BPF did not become ready before setup-delay deadline')
                if not any(r['kind']=='ready' for r in agent.records):raise RuntimeError('Host BPF preflight failed: '+agent.target)
            if time.monotonic()-started>40:raise RuntimeError('BPF setup exceeded safe setup-delay margin')
            self.state.update(status='ready',targets={a.target:a.records for a in self.agents},
                              ready_utc=datetime.now(timezone.utc).isoformat(),endpoints=ips)
            save_json(MARKER,self.state);save_json(self.directory/'ready.json',self.state)
        except BaseException:
            self.stop(require_traffic=False)
            raise

    def stop(self,require_traffic=True):
        errors=[]
        identity_error=None
        if require_traffic:
            try:
                current=json.loads(remote(GENERATED/'ssh_config',self.controller,
                    ['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf','-n',self.namespace,'get','pods','-o','json']))
                identities={p['metadata']['uid'] for p in current['items']
                            if p['spec'].get('nodeName') in (self.args.client,self.args.server)
                            and not p['spec'].get('hostNetwork',False)}
                if identities!=self.identities:identity_error='Benchmark Pod identities changed during the BPF run'
            except BaseException as error:identity_error='Cannot verify final benchmark identities: '+str(error)
        for agent in reversed(self.agents):
            try:agent.stop()
            except BaseException as error:errors.append(str(error))
        self.state.update(status='cleanup-failed' if errors else 'restored',
                          targets={a.target:a.records for a in self.agents},errors=errors)
        save_json(self.directory/'restoration.json',self.state)
        if errors:
            save_json(MARKER,self.state)
            raise RuntimeError('; '.join(errors))
        MARKER.unlink(missing_ok=True)
        if identity_error:raise RuntimeError(identity_error)
        if require_traffic:
            for agent in self.agents:
                proof=next((r for r in agent.records if r['kind']=='counters'),None)
                if not proof:raise RuntimeError('No host BPF counters on '+agent.target)
                counter='redirected' if self.args.mode=='host-vxlan-bpf-1450' else 'passed'
                if not all(a['counters'][counter]>0 for a in proof['attachments']):
                    raise RuntimeError('Selected forwarding path did not carry traffic on '+agent.target)


RECOVER = r'''
import json,subprocess,sys
s=json.loads(sys.argv[1]);errors=[];removed=[]
def run(a):return subprocess.check_output(a,text=True)
def filters(dev,direction):return json.loads(run(['tc','-j','filter','show','dev',dev,direction]))
for a in s['attachments']:
 try:
  dev=a['ingress']
  if dev!='cs740hvx' and not (dev.startswith('vnet') and dev[4:].isdigit()):raise ValueError('Unexpected recovery device')
  entries=[x for x in filters(dev,'ingress') if x.get('pref')==49174]
  ids=[x.get('options',{}).get('id') or x.get('options',{}).get('prog',{}).get('id') for x in entries]
  ids=[i for i in ids if i]
  if not ids:continue
  if ids!=[a['program_id']]:raise ValueError('Filter ID ownership changed on '+dev)
  run(['tc','filter','del','dev',dev,'ingress','pref','49174','handle','0xc740','bpf']);removed.append(dev)
 except Exception as e:errors.append(str(e))
for dev in s['created_qdiscs']:
 try:
  if dev not in {a['ingress'] for a in s['attachments']}:raise ValueError('Unproven qdisc ownership')
  q=json.loads(run(['tc','-j','qdisc','show','dev',dev]))
  if not any(x['kind']=='clsact' for x in q):continue
  if filters(dev,'ingress') or filters(dev,'egress'):raise ValueError('Other filters remain on '+dev)
  run(['tc','qdisc','del','dev',dev,'clsact'])
 except Exception as e:errors.append(str(e))
print(json.dumps({'errors':errors,'removed':removed}))
'''


def recover():
    import fcntl
    config=json.loads((ROOT/'infrastructure/config.json').read_text())
    with (GENERATED/'collection.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not MARKER.exists():
            print('No host BPF cleanup marker remains')
            return
        state=json.loads(MARKER.read_text())
        try:os.kill(state['pid'],0)
        except ProcessLookupError:pass
        else:raise RuntimeError('The owning process still exists; stop it before recovery')
        directory=Path(state['directory']).resolve()
        if not directory.is_relative_to((ROOT/'results').resolve()):raise ValueError('Recovery artifacts must be under results/')
        proof={};errors=[]
        for host in config['hypervisors']:
            path=directory/host['name']/'session.jsonl'
            if not path.exists():continue
            records=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            owned={};created=set()
            for record in records:
                if record['kind'] not in ('ready','error'):continue
                for attachment in record.get('attachments',[]):
                    if attachment.get('program_id'):owned[attachment['ingress']]=attachment
                created.update(record.get('created_qdiscs',[]))
            if not owned:
                # A successful remote finally is sufficient even without ready.
                if not any(r['kind']=='restored' and not r['errors'] for r in records):
                    errors.append('No recovery ownership proof for '+host['name'])
                continue
            target='cs740-'+host['name']+'-jump'
            try:
                result=json.loads(remote(GENERATED/'ssh_config',target,['sudo','-n','python3','-c',RECOVER,
                           json.dumps({'attachments':list(owned.values()),'created_qdiscs':sorted(created)})]))
                proof[target]=result;errors.extend(result['errors'])
            except BaseException as error:errors.append(str(error))
        save_json(directory/('recovery-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'.json'),
                  {'targets':proof,'errors':errors})
        if errors:raise RuntimeError('; '.join(errors))
        MARKER.unlink();print('Verified owned host BPF filters removed; cleanup marker cleared')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Recover only recorded host BPF filters after an interrupted experiment')
    parser.add_argument('--restore',action='store_true',required=True)
    parser.parse_args()
    recover()
