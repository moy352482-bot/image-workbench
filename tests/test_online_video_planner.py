import tempfile
import unittest
import json
from pathlib import Path
from uuid import uuid4

import httpx

from image_workbench.online_video_planner import OnlineVideoPlanner
from image_workbench.online_video_worker import OnlineVideoWorker
from image_workbench.h3_skill import H3SkillPlanner, load_h3_skill_config
from image_workbench.h3_worker import LocalVideoTransport
from tests import test_video as video_fixtures


class FakePlanner:
    def __init__(self):
        self.calls = 0

    def request(self, task):
        return {'frozen': task['prompt']}

    def run(self, request, work_dir):
        self.calls += 1
        return {'prompt': 'Skill: ' + request['frozen']}


class FakeTransport:
    identity = 'test:server'

    def __init__(self):
        self.manifest = None
        self.plan_posts = []
        self.lose_plan_ack = True

    def _path(self, manifest):
        return '/jobs/' + manifest['job_id']

    def _request(self, method, path, manifest=None, **kwargs):
        if path.endswith('/claim'):
            self.manifest = {'job_id': uuid4().hex, 'worker_id': kwargs['json']['worker_id'],
                             'claim_id': uuid4().hex, 'task': {'prompt': 'original'}}
            return {'job': self.manifest}
        self.plan_posts.append(kwargs['json']['plan'])
        if self.lose_plan_ack:
            self.lose_plan_ack = False
            raise httpx.ReadTimeout('lost acknowledgement')
        return {'ok': True}

    def heartbeat(self, manifest):
        pass


class PlannerTests(unittest.TestCase):
    def test_lost_plan_ack_reuses_frozen_result_without_reinvoking_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            transport, planner = FakeTransport(), FakePlanner()
            worker = OnlineVideoPlanner(transport, planner, Path(directory))
            with self.assertRaises(httpx.ReadTimeout):
                worker.run_once()
            worker.run_once()
            self.assertEqual(planner.calls, 1)
            self.assertEqual(transport.plan_posts, [{'prompt': 'Skill: original'}] * 2)

    def test_lost_failure_ack_reuses_failure_without_reinvoking_skill(self):
        class InvalidPlanner(FakePlanner):
            def run(self, request, work_dir):
                self.calls += 1
                raise ValueError('invalid plan')
        class FailureTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.results = []
            def complete(self, manifest, result):
                self.results.append(result)
                if len(self.results) == 1:
                    raise httpx.ReadTimeout('lost acknowledgement')
        with tempfile.TemporaryDirectory() as directory:
            transport, planner = FailureTransport(), InvalidPlanner()
            worker = OnlineVideoPlanner(transport, planner, Path(directory))
            with self.assertRaises(httpx.ReadTimeout):
                worker.run_once()
            worker.run_once()
            self.assertEqual(planner.calls, 1)
            self.assertEqual([item['status'] for item in transport.results], ['failed', 'failed'])

    def test_real_skill_plan_is_submitted_to_online_provider(self):
        fixture = video_fixtures.VideoTests('test_online_backend_validation_and_independent_queue')
        fixture.setUp()
        try:
            fixture.store.set_config(fixture.admin, {'720p': 2, '1080p': 4})
            shot = fixture.shot(backend='online-2.0')
            fixture.submit(shot)
            fields = ('mode', 'duration', 'resolution', 'aspect_ratio', 'width', 'height', 'reference_ids')
            rewritten = 'integrated_multimodal_description: organized scene\noverall_soundscape: sound\nnon_diegetic_music: music'
            class TextAdapter:
                def generate(self, **kwargs):
                    return json.dumps({'schema_version': 1, **{key: shot[key] for key in fields}, 'prompt': rewritten})

            class PlannerTransport(LocalVideoTransport):
                def _path(self, manifest):
                    return '/jobs/' + manifest['job_id']
                def _request(self, method, path, manifest=None, **kwargs):
                    if path.endswith('/claim'):
                        return {'job': self.store.claim(kwargs['json']['worker_id'], 'online-2.0', 'planning')}
                    return self.store.complete_plan(manifest['job_id'], manifest['worker_id'], manifest['claim_id'], kwargs['json']['plan'])

            requests = []
            def provider(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={'id': '123', 'status': 'pending'})
            with tempfile.TemporaryDirectory() as directory:
                transport = PlannerTransport(fixture.store)
                planner = H3SkillPlanner(TextAdapter(), config=load_h3_skill_config())
                OnlineVideoPlanner(transport, planner, Path(directory) / 'plan').run_once()
                manifest = fixture.store.claim(video_fixtures.uid(), 'online-2.0')
                worker = OnlineVideoWorker(transport, 'mock-key', video_fixtures.uid(), Path(directory) / 'video',
                    client=httpx.Client(transport=httpx.MockTransport(provider)), clock=lambda: 1000)
                worker.process(manifest)
            self.assertEqual(requests[0]['prompt'], rewritten)
            self.assertNotEqual(requests[0]['prompt'], shot['prompt'])
            self.assertEqual(requests[0]['mode'], '2.0')
            self.assertEqual(fixture.wallet()['frozen_credit'], 10)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
