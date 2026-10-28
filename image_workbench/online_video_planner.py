"""Server-side Skill planner for the online video queue."""
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx

from .atomic_files import read_json, write_json
from .domain_errors import TextGenerationError
from .locking import FileLock, LockBusyError
from .storage import _validate_id


class OnlineVideoPlanner:
    def __init__(self, transport, planner, root):
        self.transport, self.planner = transport, planner
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        identity_path = self.root / 'identity.json'
        with FileLock(self.root / 'identity.lock'):
            if identity_path.exists():
                identity = read_json(identity_path)
                if identity.get('server') != transport.identity:
                    raise ValueError('提示词执行器缓存属于另一服务器')
            else:
                identity = {'server': transport.identity, 'worker_id': uuid4().hex}
                write_json(identity_path, identity)
            self.worker_id = _validate_id(identity['worker_id'])

    def process(self, journal, path):
        manifest = journal['manifest']
        if journal.get('result'):
            self.transport.complete(manifest, journal['result'])
            journal['done'] = True
            write_json(path, journal)
            return
        if 'plan' not in journal:
            try:
                journal['plan'] = self.planner.run(journal['request'], path.parent / 'skill')
                write_json(path, journal)
            except TextGenerationError as exc:
                if exc.uncertain:
                    self.transport.complete(manifest, {'status': 'uncertain', 'error': 'Skill 调用结果待确认，不会重复调用'})
                    return
                journal['result'] = {'status': 'failed', 'error': 'Skill 提示词整理失败，未提交在线视频'}
                write_json(path, journal)
                return self.process(journal, path)
            except ValueError:
                journal['result'] = {'status': 'failed', 'error': 'Skill 输出校验失败，未提交在线视频'}
                write_json(path, journal)
                return self.process(journal, path)
        self.transport._request('POST', self.transport._path(manifest) + '/plan', manifest, json={'plan': journal['plan']})
        journal['done'] = True
        write_json(path, journal)

    def run_once(self):
        with FileLock(self.root / 'worker.lock'):
            pending = [(p, read_json(p)) for p in sorted(self.root.glob('*/journal.json'))]
            pending = [(p, j) for p, j in pending if not j.get('done')]
            if pending:
                path, journal = pending[0]
            else:
                manifest = self.transport._request('POST', '/api/worker/video/claim', json={
                    'worker_id': self.worker_id, 'backend': 'online-2.0', 'stage': 'planning'})['job']
                if not manifest:
                    return
                job = _validate_id(manifest['job_id'])
                _validate_id(manifest['claim_id'])
                if manifest.get('worker_id') != self.worker_id:
                    raise ValueError('提示词执行器身份不匹配')
                path = self.root / job / 'journal.json'
                journal = {'manifest': manifest, 'request': self.planner.request(manifest['task'])}
                write_json(path, journal)
            stop = threading.Event()
            def pulse():
                while not stop.wait(15):
                    try:
                        self.transport.heartbeat(journal['manifest'])
                    except (httpx.HTTPError, OSError, ValueError):
                        pass
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                self.process(journal, path)
            finally:
                stop.set()
                thread.join(timeout=35)

    def run(self):
        while True:
            try:
                self.run_once()
            except (httpx.HTTPError, OSError, ValueError, LockBusyError) as exc:
                print('视频提示词执行器:', type(exc).__name__, flush=True)
            time.sleep(5)
