"""Online video worker with a durable submission journal."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .atomic_files import read_json, write_bytes, write_json
from .h3_worker import RemoteVideoTransport
from .media_validation import MAX_OUTPUT_BYTES, inspect_mp4
from .storage import _validate_id


class OnlineVideoWorker:
    def __init__(self, transport, api_key, worker_id, journal_dir, *, client=None, clock=time.time):
        self.transport = transport
        self.api_key = api_key
        self.worker_id = _validate_id(worker_id)
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.clock = clock

    def save(self, path, journal):
        write_json(path, journal)

    def provider(self, method, path, **kwargs):
        return self.client.request(method, 'https://ai-genvideo.com' + path,
                                   headers={'Authorization': 'Bearer ' + self.api_key}, **kwargs)

    def process(self, manifest):
        job_id = _validate_id(manifest['job_id'])
        path = self.journal_dir / (job_id + '.json')
        journal = read_json(path) if path.exists() else None
        if journal is None:
            if manifest.get('attempt_id'):
                if not manifest.get('prompt_id'):
                    self.transport.complete(manifest, {'status': 'uncertain', 'error': '提交结果缺少供应商任务号，请人工核对'})
                    return
                journal = {'attempt_id': manifest['attempt_id'], 'provider_id': manifest['prompt_id'], 'phase': 'submitted', 'submitted_at': 0}
            else:
                journal = {'attempt_id': uuid4().hex, 'phase': 'prepared'}
            self.save(path, journal)
        attempt = journal['attempt_id']
        if journal['phase'] == 'prepared':
            self.transport.start(manifest, attempt)
            journal.update(phase='submitting', submitted_at=self.clock())
            self.save(path, journal)
            task = manifest['task']
            body = {'prompt': task['prompt'], 'mode': '2.0', 'ratio': {'landscape': '16:9', 'portrait': '9:16', 'square': '1:1'}[task['aspect_ratio']], 'durationSeconds': task['duration']}
            try:
                response = self.provider('POST', '/v1/videos/generations', json=body)
            except httpx.HTTPError:
                self.transport.complete(manifest, {'status': 'uncertain', 'error': '供应商提交结果未知，未自动重复提交'})
                return
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                journal['phase'] = 'rejected'
                self.save(path, journal)
                self.transport.complete(manifest, {'status': 'failed', 'error': f'供应商拒绝任务（HTTP {response.status_code}）'})
                return
            if response.status_code != 200:
                self.transport.complete(manifest, {'status': 'uncertain', 'error': '供应商提交结果未知，未自动重复提交'})
                return
            try:
                provider_id = str(response.json()['id'])
                if not provider_id.isdecimal() or len(provider_id) > 200:
                    raise ValueError('invalid task id')
            except (KeyError, ValueError, TypeError):
                self.transport.complete(manifest, {'status': 'uncertain', 'error': '供应商回执缺少任务号，未自动重复提交'})
                return
            journal.update(phase='submitted', provider_id=provider_id)
            self.save(path, journal)
        if journal['phase'] == 'submitting':
            self.transport.complete(manifest, {'status': 'uncertain', 'error': '提交结果未知，未自动重复提交'})
            return
        if journal['phase'] == 'rejected':
            self.transport.complete(manifest, {'status': 'failed', 'error': '供应商拒绝任务'})
            return
        self.transport.start(manifest, attempt, journal['provider_id'])
        output_path = self.journal_dir / (job_id + '.mp4')
        if journal['phase'] == 'output':
            data = output_path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != journal['sha256']:
                raise ValueError('已保存的视频校验失败')
            self.transport.upload(manifest, data, digest)
            self.transport.complete(manifest, {'status': 'succeeded', 'sha256': digest})
            output_path.unlink(missing_ok=True)
            return
        if self.clock() - journal.get('submitted_at', 0) < 300:
            self.transport.heartbeat(manifest)
            return
        response = self.provider('GET', '/v1/tasks/' + journal['provider_id'])
        response.raise_for_status()
        data = response.json()
        status = data.get('status')
        if status in {'failed', 'timeout', 'cancelled'}:
            self.transport.complete(manifest, {'status': 'failed', 'error': '供应商报告生成失败：' + status})
            return
        if status != 'succeeded':
            self.transport.heartbeat(manifest)
            return
        output_url = data.get('outputUrl')
        if not output_url:
            response = self.provider('GET', '/v1/tasks/output-url/' + journal['provider_id'])
            response.raise_for_status()
            output_url = response.json().get('outputUrl')
        try:
            data = self.download(output_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {401, 403, 404}:
                raise
            response = self.provider('GET', '/v1/tasks/output-url/' + journal['provider_id'])
            response.raise_for_status()
            data = self.download(response.json().get('outputUrl'))
        data = self.resize(data, manifest['task']['width'], manifest['task']['height'])
        digest = hashlib.sha256(data).hexdigest()
        write_bytes(output_path, data)
        journal.update(phase='output', sha256=digest)
        self.save(path, journal)
        self.transport.upload(manifest, data, digest)
        self.transport.complete(manifest, {'status': 'succeeded', 'sha256': digest})
        output_path.unlink(missing_ok=True)

    def download(self, url):
        parsed = urlsplit(url or '')
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('供应商视频地址无效')
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('供应商视频地址不可访问')
        with self.client.stream('GET', url, timeout=120, follow_redirects=False) as response:
            response.raise_for_status()
            result = bytearray()
            for chunk in response.iter_bytes(65536):
                result.extend(chunk)
                if len(result) > MAX_OUTPUT_BYTES:
                    raise ValueError('供应商视频超过大小限制')
        inspect_mp4(bytes(result))
        return bytes(result)

    @staticmethod
    def resize(data, width, height):
        with tempfile.TemporaryDirectory(prefix='online-video-') as directory:
            src, dst = Path(directory) / 'input.mp4', Path(directory) / 'output.mp4'
            src.write_bytes(data)
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(src), '-vf',
                            f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(dst)],
                           check=True, timeout=300, capture_output=True)
            if dst.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError('处理后视频超过大小限制')
            result = dst.read_bytes()
            inspect_mp4(result)
            return result

    def run(self):
        while True:
            try:
                manifest = self.transport._request('POST', '/api/worker/video/claim', json={'worker_id': self.worker_id, 'backend': 'online-2.0'})['job']
                if manifest:
                    self.process(manifest)
            except Exception as exc:
                # Exceptions can contain credentials or signed URLs; keep logs to their type only.
                print('online video worker:', type(exc).__name__, flush=True)
            time.sleep(30)


def main():
    config = json.loads(Path(os.environ['IMAGE_WORKBENCH_ONLINE_VIDEO_CONFIG']).read_text(encoding='utf-8'))
    worker = OnlineVideoWorker(RemoteVideoTransport(config['server'], config['worker_token']), config['api_key'],
                               config['worker_id'], config['journal_dir'])
    worker.run()


if __name__ == '__main__':
    main()
