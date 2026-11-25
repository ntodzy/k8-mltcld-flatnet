#!/usr/bin/env python3
"""Change only an experiment NIC's virtio queue driver; preserve its old driver."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def driver_xml(xml, mac, queues=None, original=None, restore=False):
    root = ET.fromstring(xml)
    interfaces = [interface for interface in root.findall('./devices/interface')
                  if interface.find('mac') is not None and interface.find('mac').get('address', '').lower() == mac.lower()]
    if len(interfaces) != 1 or interfaces[0].find('model').get('type') != 'virtio':
        raise ValueError('Expected exactly one virtio experiment interface matching the configured MAC')
    interface = interfaces[0]
    previous = interface.find('driver')
    previous_xml = ET.tostring(previous, encoding='unicode') if previous is not None else None
    if queues is not None and not 1 <= queues <= int(root.findtext('vcpu')):
        raise ValueError('Queue count must be positive and no greater than guest vCPUs')
    if restore:
        if previous is not None:
            interface.remove(previous)
        if original is not None:
            interface.append(ET.fromstring(original))
    else:
        if previous is None:
            previous = ET.SubElement(interface, 'driver')
        previous.set('name', 'vhost')
        previous.set('queues', str(queues))
    return ET.tostring(root, encoding='unicode'), previous_xml


def virsh(*argv):
    return subprocess.check_output(['virsh', *argv], text=True).strip()


def main():
    domain, mac, requested = sys.argv[1:]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', domain):
        raise ValueError('Invalid domain name')
    before = virsh('dumpxml', domain, '--inactive')
    root = ET.fromstring(before)
    backup_dir = Path('/var/lib/cs740-network/queues')
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'{domain}-{mac.replace(":", "")}.json'
    if requested == 'restore':
        saved = json.loads(backup.read_text())
        if saved['uuid'] != root.findtext('uuid'):
            raise ValueError('Queue baseline belongs to a different VM UUID')
        after, _ = driver_xml(before, mac, original=saved['driver'], restore=True)
    else:
        after, previous = driver_xml(before, mac, queues=int(requested))
        if not backup.exists():
            backup.write_text(json.dumps({'uuid': root.findtext('uuid'), 'driver': previous}))
            backup.chmod(0o600)
    changed = ET.tostring(ET.fromstring(before)) != ET.tostring(ET.fromstring(after))
    if changed:
        virsh('shutdown', domain)
        deadline = time.monotonic() + 120
        while virsh('domstate', domain) != 'shut off':
            if time.monotonic() >= deadline:
                raise RuntimeError('Guest did not shut down gracefully; no forced destroy was attempted')
            time.sleep(2)
        xml_path = backup_dir / f'{domain}-pending.xml'
        xml_path.write_text(after)
        try:
            virsh('define', str(xml_path))
            virsh('start', domain)
        except BaseException:
            # Restore the previous persistent definition and bring the guest back.
            xml_path.write_text(before)
            virsh('define', str(xml_path))
            if virsh('domstate', domain) == 'shut off':
                virsh('start', domain)
            raise
        finally:
            xml_path.unlink(missing_ok=True)
    print(json.dumps({'domain': domain, 'mac': mac, 'requested': requested, 'changed': changed,
                      'before_xml': before, 'after_xml': virsh('dumpxml', domain, '--inactive'),
                      'baseline': str(backup)}))


if __name__ == '__main__':
    main()
