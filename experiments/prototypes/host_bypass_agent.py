"""Lifetime-scoped, flow-specific L2 bridge shortcut on existing managed hosts."""
import base64
import json
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

PREF = '49174'
HANDLE = '0xc740'


def run(spec):
    owned, created = [], []
    def command(argv, check=True):
        r = subprocess.run(argv, text=True, capture_output=True, timeout=20)
        if check and r.returncode:
            raise RuntimeError(f'{argv}: {r.stderr}')
        return r
    def data(argv):return json.loads(command(argv).stdout)
    def filters(dev):return data(['tc','-j','filter','show','dev',dev,'ingress'])
    def pid(entries):
        found = [x.get('options',{}).get('id') or x.get('options',{}).get('prog',{}).get('id')
                 for x in entries if x.get('pref')==int(PREF)]
        found = [i for i in found if i]
        if len(found)!=1:raise RuntimeError('Owned filter ID is ambiguous')
        return found[0]
    def emit(record):print(json.dumps(record),flush=True)
    failure = None
    try:
        links = {x['ifname']:x for x in data(['ip','-j','-d','link','show'])}
        if links['cs740hvx'].get('master') not in ('cs740br',links['cs740br']['ifindex']):
            raise RuntimeError('Host VXLAN is not attached to the managed bridge')
        if links['cs740br']['mtu']!=1450 or links['cs740hvx']['mtu']!=1450:
            raise RuntimeError('Host experiment must use inner MTU 1450')
        xml = ET.fromstring(command(['virsh','-c','qemu:///system','dumpxml',spec['domain']]).stdout)
        interfaces = [i for i in xml.findall('./devices/interface')
                      if i.get('type')=='bridge' and i.find('source').get('bridge')=='cs740br'
                      and i.find('mac').get('address').lower()==spec['mac'].lower()]
        if len(interfaces)!=1:raise RuntimeError('Managed VM experiment TAP is ambiguous')
        tap = interfaces[0].find('target').get('dev')
        if links[tap].get('master') not in ('cs740br',links['cs740br']['ifindex']):
            raise RuntimeError('Managed TAP is not a bridge port')
        # No replacement of existing filters, qdiscs or TCX attachments.
        net = data(['bpftool','-j','net'])
        for block in net:
            if any(x.get('devname') in (tap,'cs740hvx') and 'tcx' in x.get('kind','')
                   for x in block.get('tc',[])):
                raise RuntimeError('Unexpected TCX attachment on a selected host port')
        plans = [(tap,'cs740hvx',spec['local_ip'],spec['remote_ip']),
                 ('cs740hvx',tap,spec['remote_ip'],spec['local_ip'])]
        emit({'kind':'preflight','tap':tap,'links':links,'hooks':net})
        with tempfile.TemporaryDirectory(prefix='cs740-host-bpf-') as tmp:
            obj = Path(tmp)/'redirect.o';obj.write_bytes(base64.b64decode(spec['object']))
            for ingress, output, source, destination in plans:
                if filters(ingress):raise RuntimeError('Refusing existing classic ingress filters on '+ingress)
                qdiscs = data(['tc','-j','qdisc','show','dev',ingress])
                if not any(x['kind']=='clsact' for x in qdiscs):
                    if any(x['kind']=='ingress' for x in qdiscs):raise RuntimeError('Existing ingress qdisc')
                    command(['tc','qdisc','add','dev',ingress,'clsact']);created.append(ingress)
                command(['tc','filter','add','dev',ingress,'ingress','pref',PREF,
                         'handle',HANDLE,'bpf','da','obj',str(obj),'sec','classifier'])
                # Register immediately, so failures during ID/map discovery still detach.
                record={'ingress':ingress,'output':output,'program_id':None}
                owned.append(record)
                record['program_id']=pid(filters(ingress))
                info=data(['bpftool','-j','prog','show','id',str(record['program_id'])])
                if isinstance(info,list):info=info[0]
                if len(info['map_ids'])!=1:raise RuntimeError('Unexpected map ownership')
                record['map_id']=info['map_ids'][0]
                key=struct.pack('<II',*[int.from_bytes(bytes(map(int,ip.split('.'))),'big') for ip in (source,destination)])
                record['key']=list(key)
                value=struct.pack('<IIQQQ',links[output]['ifindex'],int(spec['bypass']),0,0,0)
                command(['bpftool','map','update','id',str(record['map_id']),'key','hex',
                         *[f'{b:02x}' for b in key],'value','hex',*[f'{b:02x}' for b in value]])
            emit({'kind':'ready','attachments':owned,'created_qdiscs':created})
            select.select([sys.stdin],[],[],spec['lifetime_seconds'])
            for record in owned:
                raw=data(['bpftool','-j','map','lookup','id',str(record['map_id']),
                          'key','hex',*[f'{b:02x}' for b in record['key']]])
                values=bytes(int(x,16) if isinstance(x,str) else x for x in raw['value'])
                record['counters']=dict(zip(('matched','redirected','passed'),struct.unpack_from('<QQQ',values,8)))
            emit({'kind':'counters','attachments':owned})
    except BaseException as error:
        failure=str(error);emit({'kind':'error','error':failure,'attachments':owned,'created_qdiscs':created})
    finally:
        errors=[]
        for record in reversed(owned):
            try:
                current=pid(filters(record['ingress']))
                if record['program_id'] is not None and current!=record['program_id']:
                    raise RuntimeError('Filter ownership changed; refusing to delete it')
                command(['tc','filter','del','dev',record['ingress'],'ingress','pref',PREF,'handle',HANDLE,'bpf'])
            except BaseException as error:errors.append(str(error))
        for dev in reversed(created):
            try:
                # Remove an owned qdisc only if nobody added other filters.
                if not filters(dev) and not data(['tc','-j','filter','show','dev',dev,'egress']):
                    command(['tc','qdisc','del','dev',dev,'clsact'])
                else:raise RuntimeError('Owned qdisc now has other filters: '+dev)
            except BaseException as error:errors.append(str(error))
        emit({'kind':'restored','errors':errors})
    return 1 if failure or errors else 0


if __name__=='__main__':
    def stop(signum,frame):raise RuntimeError(f'Signal {signum}')
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGHUP,stop)
    sys.exit(run(json.loads(sys.stdin.readline())))
