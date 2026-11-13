#!/usr/bin/env python3
"""Measure a batch of video jobs through the authenticated Titry API."""
import argparse
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from statistics import mean
from uuid import UUID


class BenchmarkClient:
    def __init__(self, base_url, token, ca=None):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.context = ssl.create_default_context(cafile=ca)

    def request(self, path, body=None, headers=None):
        request = urllib.request.Request(
            self.base_url + path, data=body,
            headers={'Accept': 'application/json', **(headers or {})},
        )
        request.add_unredirected_header('Authorization', f'Token {self.token}')
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'{path}: HTTP {exc.code}: {exc.read(2048).decode(errors="replace")}') from exc

    def submit(self, path, index):
        boundary = secrets.token_hex(16)
        # Use a safe header filename while retaining the original basename in the report.
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="video"; '
                  f'filename="clip-{index}{path.suffix.lower()}"\r\n\r\n').encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        with path.open('rb') as source:
            size = os.fstat(source.fileno()).st_size
            chunks = iter(lambda: source.read(1024 * 1024), b'')
            return self.request('/api/jobs/', chain((prefix,), chunks, (suffix,)), {
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(prefix) + size + len(suffix)),
            })


def run_batch(client, videos, expected_nodes, timeout):
    snapshot = client.request('/api/operations/')
    ready = {node['name'] for node in snapshot['nodes'] if node['available']}
    if ready != expected_nodes or snapshot['waiting'] or snapshot['running']:
        raise RuntimeError(f'Expected idle workers {sorted(expected_nodes)}, found {sorted(ready)}; '
                           f'queued={snapshot["waiting"]}, running={snapshot["running"]}')

    submitted = [client.submit(path, index) for index, path in enumerate(videos, start=1)]
    pending = {str(UUID(job['id'])) for job in submitted}
    finished = {}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for job_id in sorted(pending):
            job = client.request(f'/api/jobs/{job_id}/')
            if job['status'] in ('failed', 'cancelled'):
                raise RuntimeError(f'{job_id}: {job["status"]}: {job["error"]}')
            if job['status'] == 'succeeded':
                finished[job_id] = job
                pending.remove(job_id)
        if pending:
            time.sleep(0.5)
    if pending:
        raise RuntimeError(f'Timed out waiting for {", ".join(sorted(pending))}')

    jobs = []
    for path, submission in zip(videos, submitted, strict=True):
        job = finished[submission['id']]
        if not all(job['artifacts'].get(kind) for kind in ('srt', 'mp4')):
            raise RuntimeError(f'{job["id"]}: missing result links')
        created, started, ended = (datetime.fromisoformat(job[key]) for key in ('created_at', 'started_at', 'finished_at'))
        jobs.append({
            'id': job['id'], 'video': path.name, 'duration_seconds': job['duration_seconds'], 'node': job['node'],
            'created_at': created.isoformat(), 'started_at': started.isoformat(), 'finished_at': ended.isoformat(),
            'queue_seconds': (started - created).total_seconds(), 'processing_seconds': (ended - started).total_seconds(),
        })
    per_node = Counter(job['node'] for job in jobs)
    if set(per_node) != expected_nodes:
        raise RuntimeError(f'Expected jobs on {sorted(expected_nodes)}, got {dict(per_node)}')
    earliest = min(datetime.fromisoformat(job['created_at']) for job in jobs)
    latest = max(datetime.fromisoformat(job['finished_at']) for job in jobs)
    return {
        'recorded_at': datetime.now(timezone.utc).isoformat(), 'workers': sorted(expected_nodes),
        'batch_seconds': (latest - earliest).total_seconds(),
        'mean_queue_seconds': mean(job['queue_seconds'] for job in jobs),
        'max_queue_seconds': max(job['queue_seconds'] for job in jobs),
        'jobs_per_node': dict(per_node), 'jobs': jobs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--ca', help='CA certificate for the HTTPS server')
    parser.add_argument('--workers', nargs='+', required=True, help='Exact list of worker names expected for this run')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('videos', nargs='+', type=Path)
    args = parser.parse_args()
    token = os.environ.get('TITRY_BENCHMARK_TOKEN')
    if not token:
        parser.error('Set TITRY_BENCHMARK_TOKEN to a staff API token.')
    try:
        report = run_batch(BenchmarkClient(args.url, token, args.ca), args.videos, set(args.workers), args.timeout)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
