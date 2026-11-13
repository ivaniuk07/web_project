#!/usr/bin/env python3
"""Real HTTP rehearsal. Run against a private demonstration deployment, with its CA."""
import argparse
import http.cookiejar
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from itertools import chain, repeat
from pathlib import Path


class Browser:
    def __init__(self, base, context):
        self.base = base.rstrip('/')
        self.origin = '{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(base))
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), urllib.request.HTTPSHandler(context=context))

    def request(self, path, data=None, content_type=None, csrf=True, *, content_length=None):
        headers = {}
        if data is not None:
            if csrf:
                token = next((c.value for c in self.cookies if c.name == 'csrftoken'), None)
                if token:
                    headers['X-CSRFToken'] = token
            headers.update(Origin=self.origin, Referer=self.base+'/')
            headers['Content-Type'] = content_type or 'application/x-www-form-urlencoded'
        if content_length is not None:
            headers['Content-Length'] = str(content_length)
        request = urllib.request.Request(self.base+path, data=data, headers=headers)
        try:
            response = self.client.open(request, timeout=180)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.geturl(), response.read()

    def get(self, path):
        return self.request(path)

    def post(self, path, values=None, csrf=True):
        return self.request(path, urllib.parse.urlencode(values or {}).encode(), csrf=csrf)

    def upload(self, data, filename='clip.mp4', *, size=None):
        boundary = 'subtitle-'+secrets.token_hex(12)
        prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{filename}"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        if size is None:
            size, data = len(data), (data,)
        body = chain((prefix,), data, (suffix,))
        return self.request('/', body, 'multipart/form-data; boundary='+boundary,
                            content_length=len(prefix) + size + len(suffix))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--ca', help='Trusted local CA PEM; certificate verification remains enabled.')
    parser.add_argument('--credentials', required=True, help='Private JSON containing username/password for an existing staff demo account.')
    parser.add_argument('--video', required=True)
    parser.add_argument('--long-video', required=True)
    parser.add_argument('--too-long-video', required=True)
    parser.add_argument('--max-upload-bytes', type=int, default=1024 ** 3)
    parser.add_argument('--pause-for-scaling', action='store_true', help='Pause with a busy pair for the operator to run the separate scaler.')
    parser.add_argument('--report', default='.superpowers/stage8/rehearsal.json')
    args = parser.parse_args()
    private = Path(args.credentials)
    if private.stat().st_mode & 0o077:
        parser.error('Credentials file must have mode 600.')
    credentials = json.loads(private.read_text())
    context = ssl.create_default_context(cafile=args.ca)
    admin = Browser(args.url, context)
    events = []
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'url': args.url, 'username': credentials['username'], 'events': events}
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)

    def record(name, **data):
        events.append({'check': name, 'time': datetime.now(timezone.utc).isoformat(), **data})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(name, json.dumps(data, ensure_ascii=False), flush=True)

    def upload(browser, data, name='clip.mp4'):
        status, url, _ = browser.upload(data, name)
        found = re.search(r'/jobs/([0-9a-f-]{36})/', url)
        assert status == 200 and found, 'Valid upload rejected'
        return found.group(1)

    def detail(browser, job):
        status, _, body = browser.get(f'/jobs/{job}/')
        assert status == 200, 'Job page inaccessible'
        text = body.decode()
        node = re.search(r'Вузол — ([^<]+)', text)
        return text, node.group(1) if node else None

    def wait_success(browser, jobs, expected_nodes=None):
        overlapped = False
        for _ in range(900):
            states = [detail(browser, job) for job in jobs]
            overlapped |= all('Стан — Обробляється' in text for text, _ in states)
            assert not any('Стан — Помилка' in text for text, _ in states), 'Conversion failed'
            if all('Стан — Завершено' in text for text, _ in states):
                if expected_nodes:
                    assert {node for _, node in states} == set(expected_nodes), 'Unexpected routing'
                return states, overlapped
            time.sleep(.2)
        raise AssertionError('Timed out waiting for conversion')

    try:
        assert admin.get('/accounts/login/')[0] == 200
        assert admin.post('/accounts/login/', credentials, csrf=False)[0] == 403
        status, url, _ = admin.post('/accounts/login/', credentials)
        assert status == 200 and '/accounts/login/' not in url
        assert any(c.name == 'sessionid' and c.secure for c in admin.cookies) if args.url.startswith('https:') else True
        record('login_csrf_and_session', secure_cookie=args.url.startswith('https:'))
        member = Browser(args.url, context)
        member.get('/accounts/register/')
        password = secrets.token_urlsafe(24)
        username = 'defense-user-'+secrets.token_hex(4)
        status, url, _ = member.post('/accounts/register/', {'username':username,'password1':password,'password2':password})
        assert status == 200 and '/accounts/register/' not in url
        assert member.get('/operations/')[0] == 403
        assert admin.get('/operations/')[0] == 200
        record('registration_and_staff_permissions', member=username)
        for _ in range(120):
            page = admin.get('/operations/')[2].decode()
            if all(f'<td>{node}</td><td>так</td>' in page for node in ['node-1', 'node-2']): break
            time.sleep(.25)
        else: raise AssertionError('Both base workers must be available')

        short = Path(args.video).read_bytes()
        jobs = [upload(admin, short, f'defense-{i}.mp4') for i in (1, 2)]
        for job in jobs:
            admin.post(f'/process/{job}/')
        states, overlap = wait_success(admin, jobs, ['node-1','node-2'])
        assert overlap, 'No concurrent RUNNING states observed'
        for job in jobs:
            status, _, srt = admin.get(f'/download/{job}/')
            assert status == 200 and b'-->' in srt and len(srt) > 35
            assert admin.get(f'/video/{job}/?download=1')[0] == 200
            assert member.get(f'/jobs/{job}/')[0] == 404
        status, _, history_body = admin.get('/history/')
        assert status == 200
        history = history_body.decode()
        assert all(job in history for job in jobs)
        record('full_flow_two_nodes', jobs=jobs, nodes=[n for _, n in states], simultaneous_running=overlap, history=True, mp4=True, srt=True)
        for data, name in [(b'broken video','broken.mp4'),(short,'bad.txt'),(Path(args.too_long_video).read_bytes(),'too-long.mp4')]:
            status, url, _ = admin.upload(data, name)
            assert status == 200 and '/jobs/' not in url
        oversized_size = args.max_upload_bytes + 1
        block_size = 1024 * 1024
        full_blocks, remainder = divmod(oversized_size, block_size)
        oversized = chain(repeat(b'0' * block_size, full_blocks), (b'0' * remainder,))
        status, url, _ = admin.upload(oversized, 'oversized.mp4', size=oversized_size)
        assert status == 200 and '/jobs/' not in url
        record('invalid_file_extension_duration_and_size_rejected', size_limit=args.max_upload_bytes)
        waiting = [upload(admin, short, f'quota-{i}.mp4') for i in range(3)]
        status, url, _ = admin.upload(short, 'quota-exceeded.mp4')
        assert status == 200 and '/jobs/' not in url
        for job in waiting:
            admin.post(f'/cancel/{job}/')
            assert 'Стан — Скасовано' in detail(admin, job)[0]
        record('active_quota_and_queued_cancellation', cancelled=waiting)
        long_video = Path(args.long_video).read_bytes()
        job = upload(admin, long_video, 'cancel-running.mp4')
        admin.post(f'/process/{job}/')
        for _ in range(120):
            if 'Стан — Обробляється' in detail(admin, job)[0]: break
            time.sleep(.05)
        else: raise AssertionError('No RUNNING state to cancel')
        admin.post(f'/cancel/{job}/')
        time.sleep(.5)
        assert 'Стан — Скасовано' in detail(admin, job)[0]
        assert admin.get(f'/video/{job}/')[0] == 404
        assert admin.get(f'/download/{job}/')[0] == 404
        record('running_cancellation_blocks_results', job=job)

        if args.pause_for_scaling:
            busy = [upload(admin, long_video, f'pressure-{i}.mp4') for i in (1,2)]
            for job in busy: admin.post(f'/process/{job}/')
            for _ in range(120):
                if all('Стан — Обробляється' in detail(admin, j)[0] for j in busy): break
                time.sleep(.05)
            else: raise AssertionError('Both base workers must be busy')
            queued = upload(admin, short, 'pressure-queued.mp4')
            admin.post(f'/process/{queued}/')
            assert 'приблизно' in detail(admin, queued)[0]
            eta_text = detail(admin, queued)[0]
            eta = re.search(r'приблизно (\d+) с', eta_text)
            record('busy_queue_eta', jobs=busy+[queued], seconds=int(eta.group(1)) if eta else None)
            cancelled = upload(member, short, 'cancel-enqueued.mp4')
            member.post(f'/process/{cancelled}/')
            assert 'Стан — Очікує обробки' in detail(member, cancelled)[0]
            member.post(f'/cancel/{cancelled}/')
            assert 'Стан — Скасовано' in detail(member, cancelled)[0]
            assert member.get(f'/video/{cancelled}/')[0] == 404
            record('broker_queue_cancellation', job=cancelled)
            print('WAIT_FOR_SCALING', flush=True)
            input()  # Operator runs capped host-side experiment, then confirms completion.
            third = upload(member, short, 'node-three.mp4')
            member.post(f'/process/{third}/')
            wait_success(member, [third], ['node-3'])
            wait_success(admin, busy+[queued])
            record('third_node_real_conversion', job=third, node='node-3')
        admin.post('/accounts/logout/')
        assert '/accounts/login/' in admin.get('/')[1]
        record('post_logout')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['passed'] = True
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('REHEARSAL_OK', flush=True)
    except Exception:
        report['passed'] = False
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        raise


if __name__ == '__main__':
    main()
