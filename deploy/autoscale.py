#!/usr/bin/env python3
"""Bounded host-side experiment. Never grant the web container Docker access."""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path


def scale_once(gateway, max_nodes=3, apply=False):
    if type(max_nodes) is not int or max_nodes not in (2, 3):
        raise ValueError('This experiment supports a maximum of 2 or 3 nodes.')
    snapshot = gateway.snapshot()
    if not isinstance(snapshot, dict) or type(snapshot.get('waiting')) is not int or snapshot['waiting'] < 0:
        raise ValueError('Invalid queue telemetry; no services started.')
    nodes = snapshot.get('nodes')
    if not isinstance(nodes, list):
        raise ValueError('Missing node telemetry.')
    names = set()
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get('name'), str)
                or node['name'] in names or type(node.get('available')) is not bool
                or any(type(node.get(key)) is not int or node[key] < 0 for key in ('running', 'queued'))):
            raise ValueError('Invalid node telemetry; no services started.')
        names.add(node['name'])
    if names != {'node-1', 'node-2', 'node-3'}:
        raise ValueError('Configure exactly node-1, node-2 and reserved node-3 before scaling.')
    if max_nodes == 2:
        return {'decision': 'at-limit', 'max_nodes': 2}
    live = [node for node in nodes if node['available']]
    if len(live) >= max_nodes or 'worker3' in gateway.running_services():
        return {'decision': 'at-limit', 'max_nodes': max_nodes}
    base = [node for node in live if node['name'] in {'node-1', 'node-2'}]
    if len(base) < 2 or any(node['running'] < 1 for node in base) or not snapshot['waiting']:
        return {'decision': 'no-pressure', 'max_nodes': max_nodes}
    if apply:
        gateway.start_extra()
    return {'decision': 'started-node-3' if apply else 'would-start-node-3', 'max_nodes': max_nodes}


class ComposeGateway:
    def __init__(self, files, env_file):
        self.command = ['docker', 'compose', '--env-file', env_file]
        for file in files:
            self.command.extend(['-f', file])

    def _run(self, args):
        # Capture failures: Docker's stderr can contain expanded private configuration.
        result = subprocess.run(self.command + args, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('Docker Compose command failed; inspect local service health.')
        return result.stdout

    def snapshot(self):
        return json.loads(self._run(['exec', '-T', 'web', 'python', 'manage.py', 'capacity_snapshot']))

    def running_services(self):
        return set(self._run(['ps', '--status', 'running', '--services']).splitlines())

    def start_extra(self):
        self._run(['up', '-d', '--no-build', 'redis3', 'worker3'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-f', '--compose-file', action='append', required=True)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--max-nodes', type=int, choices=(2, 3), default=3)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Opt in to starting redis3/worker3; default is dry-run.')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('--interval must be at least 1 second')
    root = Path(__file__).resolve().parents[1] / '.superpowers'
    root.mkdir(exist_ok=True)
    try:
        with (root / 'autoscale.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gateway = ComposeGateway(args.compose_file, args.env_file)
            while True:
                print(json.dumps(scale_once(gateway, args.max_nodes, args.apply)), flush=True)
                if args.once:
                    break
                time.sleep(args.interval)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print('Scaling check failed or another controller owns the lock. No further actions taken.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
