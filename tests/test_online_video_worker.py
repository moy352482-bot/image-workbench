import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx

from image_workbench.online_video_worker import OnlineVideoWorker


def uid():
    return uuid4().hex


class Transport:
    def __init__(self):
        self.started = []
        self.completed = []
        self.uploaded = []
        self.fail_upload = False

    def start(self, manifest, attempt, prompt_id=None):
        self.started.append((attempt, prompt_id))

    def heartbeat(self, manifest):
        pass

    def upload(self, manifest, data, digest):
        self.uploaded.append((data, digest))
        if self.fail_upload:
            self.fail_upload = False
            raise ConnectionError('lost acknowledgement')

    def complete(self, manifest, result):
        self.completed.append(result)


class OnlineWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Transport()
        self.requests = []
        self.fail_post = False
        self.now = 1000
        self.client = httpx.Client(transport=httpx.MockTransport(self.response))
        self.worker = OnlineVideoWorker(self.transport, 'test-key', uid(), Path(self.temp.name), client=self.client, clock=lambda: self.now)
        self.manifest = {'job_id': uid(), 'worker_id': uid(), 'claim_id': uid(), 'attempt_id': None, 'prompt_id': None,
                         'task': {'prompt': 'red square', 'aspect_ratio': 'landscape', 'duration': 5, 'width': 1280, 'height': 720}}

    def response(self, request):
        self.requests.append((request.method, request.url.path))
        if request.method == 'POST':
            if self.fail_post:
                raise httpx.ReadTimeout('uncertain submission', request=request)
            return httpx.Response(200, json={'id': '123', 'status': 'pending'})
        return httpx.Response(200, json={'status': 'succeeded', 'outputUrl': 'https://example.com/video.mp4'})

    def test_uncertain_post_never_submits_twice_after_restart(self):
        self.fail_post = True
        self.worker.process(self.manifest)
        self.assertEqual(self.transport.completed[-1]['status'], 'uncertain')
        self.worker.process(self.manifest)
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 1)

    def test_recover_known_task_and_retry_same_output_after_lost_ack(self):
        self.worker.process(self.manifest)
        self.assertEqual(self.requests, [('POST', '/v1/videos/generations')])
        self.now += 301
        self.worker.download = lambda url: b'provider video'
        self.worker.resize = lambda data, width, height: b'processed video'
        self.transport.fail_upload = True
        with self.assertRaises(ConnectionError):
            self.worker.process(self.manifest)
        self.worker.download = lambda url: self.fail('should reuse saved output')
        self.worker.process(self.manifest)
        self.assertEqual(self.transport.uploaded[0], self.transport.uploaded[1])
        self.assertEqual(self.transport.completed[-1]['status'], 'succeeded')
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 1)
        self.assertEqual(self.transport.started[-1][1], '123')


if __name__ == '__main__':
    unittest.main()
