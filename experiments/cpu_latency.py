"""Scoped CPU latency QoS requests; closing the descriptor restores the policy."""
import json
import subprocess
import threading

AGENT = r'''
import json,os,select,signal,struct,sys
from pathlib import Path
def idle():
    return {str(p):{'name':(p/'name').read_text().strip(),'latency_us':int((p/'latency').read_text()),
                   'usage':int((p/'usage').read_text()),'time_us':int((p/'time').read_text())}
            for p in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpuidle/state*')}
def current():
    fd=os.open('/dev/cpu_dma_latency',os.O_RDONLY)
    try:return struct.unpack('i',os.read(fd,4))[0]
    finally:os.close(fd)
def stop(signum,frame):raise SystemExit(128+signum)
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGHUP,stop)
limit=int(sys.argv[1]);before=current();fd=os.open('/dev/cpu_dma_latency',os.O_RDWR)
try:
    os.write(fd,struct.pack('i',limit))
    effective=current()
    if limit>=0 and effective>limit:raise ValueError('CPU latency request was not effective')
    print(json.dumps({'kind':'ready','before_us':before,'requested_us':limit,'effective_us':effective,'idle':idle()}),flush=True)
    select.select([sys.stdin],[],[],600)
finally:
    after=idle();os.close(fd)
    print(json.dumps({'kind':'restored','effective_us':current(),'expected_us':before,'idle':after}),flush=True)
'''


class CPULatencyRequest:
    def __init__(self, target, limit, directory):
        from common import GENERATED, ssh_command
        self.directory = directory
        directory.mkdir(parents=True)
        self.records, self.ready = [], threading.Event()
        self.stderr = (directory / 'stderr.log').open('w')
        self.process = subprocess.Popen(ssh_command(GENERATED / 'ssh_config', target,
                         ['sudo', '-n', 'python3', '-u', '-c', AGENT, str(limit)]),
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True)
        def read():
            try:
                with (directory / 'request.jsonl').open('w') as output:
                    for line in self.process.stdout:
                        record = json.loads(line); self.records.append(record)
                        output.write(line); output.flush()
                        if record['kind'] == 'ready':self.ready.set()
            finally:self.ready.set()
        self.reader = threading.Thread(target=read, daemon=True);self.reader.start()

    def stop(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.write('stop\n');self.process.stdin.flush()
                self.process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill();self.process.wait()
        finally:
            self.reader.join(timeout=10)
            self.process.stdin.close();self.process.stdout.close();self.stderr.close()
        restored = next((r for r in self.records if r['kind'] == 'restored'), None)
        if restored is None or restored['effective_us'] != restored['expected_us']:
            raise RuntimeError('CPU latency QoS restoration was not verified; inspect ' + str(self.directory))


def requests(limit, directory):
    from latency_study import targets
    result = []
    try:
        for label, (target, domain, _) in targets().items():
            if domain:
                request = CPULatencyRequest(target, limit, directory / label)
                result.append(request)
        for request in result:
            request.ready.wait(60)
            if not any(r['kind'] == 'ready' for r in request.records):
                raise RuntimeError('CPU latency QoS request failed; inspect ' + str(request.directory))
            observation = next(r for r in request.records if r['kind'] == 'ready')
            if limit >= 0 and observation['before_us'] <= limit:
                raise RuntimeError('CPU latency was already constrained; this would not be a policy contrast')
        return result
    except BaseException:
        close_requests(result)
        raise


def close_requests(holders):
    failures = []
    for holder in holders:
        try:holder.stop()
        except BaseException as error:failures.append(str(error))
    if failures:
        raise RuntimeError('; '.join(failures))
