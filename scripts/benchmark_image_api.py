"""显式授权后，对同一图片请求测 1/2/4 路并发；每请求只调用一次。"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import statistics
import sys
import threading
import time

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    from image_workbench.image_api_backend import ImageAPIBackend
    from image_workbench.image_prompting import default_image_skill
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--levels', nargs='+', type=int, choices=(1, 2, 4), default=[1, 2, 4])
    parser.add_argument('--allow-image-generation', action='store_true')
    args = parser.parse_args(argv)
    if not args.allow_image_generation:
        parser.error('真实 API 测试需要 --allow-image-generation；默认会生成 7 张图片')
    if args.levels != sorted(set(args.levels)):
        parser.error('并发档位必须从小到大，不能重复')
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    backend = ImageAPIBackend(config['base_url'], config['api_key'], model=config.get('model', 'gpt-image-2'))
    prompt = '产品摄影：一只蓝色陶瓷马克杯放在米白色桌面上，右侧圆弧把手，浅灰背景，左上方柔和自然光，完整展示杯子，无文字、无图案、无商标。'
    compiled = default_image_skill().compile(prompt, aspect_ratio='1:1')['compiled_prompt']
    payload = {'model': backend.model, 'prompt': compiled, 'n': 1, 'response_format': 'b64_json',
               'size': '1024x1024', 'quality': 'auto', 'stream': False, 'history_disabled': True}
    content = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'provider': backend.base_url,
              'model': backend.model, 'source_prompt': prompt, 'request_sha256': hashlib.sha256(content).hexdigest(),
              'automatic_retries': 0, 'planned_calls': sum(args.levels), 'rounds': []}
    (args.output / 'request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def save():
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    for level in args.levels:
        barrier = threading.Barrier(level)
        started = time.monotonic()
        result = {'concurrency': level, 'requests': []}
        report['rounds'].append(result)
        save()
        print(json.dumps({'event': 'round_started', 'concurrency': level}), flush=True)

        def call(index, *, barrier=barrier, started=started, level=level):
            entry = {'index': index, 'success': False}
            with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
                barrier.wait(timeout=30)
                request_started = time.monotonic()
                entry['started_seconds'] = round(request_started - started, 3)
                try:
                    response = client.post(backend.base_url + '/v1/images/generations', content=content,
                        headers={'Authorization': 'Bearer ' + backend.api_key, 'Content-Type': 'application/json'})
                    entry['http_status'] = response.status_code
                    data = response.json()
                    if not response.is_success:
                        error = data.get('error') if isinstance(data, dict) else None
                        entry['error_code'] = error.get('code') if isinstance(error, dict) else None
                        return entry
                    images = data.get('data')
                    if not isinstance(images, list) or len(images) != 1:
                        raise ValueError('expected one image')
                    raw = base64.b64decode(images[0]['b64_json'], validate=True)
                    with Image.open(io.BytesIO(raw)) as image:
                        entry['width'], entry['height'] = image.size
                        suffix = '.jpg' if image.format == 'JPEG' else '.webp' if image.format == 'WEBP' else '.png'
                        image.verify()
                    name = f'concurrency-{level}-{index}{suffix}'
                    (args.output / name).write_bytes(raw)
                    entry.update(success=True, image=name, image_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                                 usage=data.get('usage'))
                except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as error:
                    entry['error_type'] = type(error).__name__
                finally:
                    entry['finished_seconds'] = round(time.monotonic() - started, 3)
                    entry['elapsed_seconds'] = round(time.monotonic() - request_started, 3)
            return entry

        with ThreadPoolExecutor(max_workers=level) as pool:
            futures = [pool.submit(call, index + 1) for index in range(level)]
            for future in as_completed(futures):
                entry = future.result()
                result['requests'].append(entry)
                save()
                print(json.dumps({'event': 'request_finished', 'concurrency': level,
                    **{k: v for k, v in entry.items() if k not in {'usage', 'sha256'}}}, ensure_ascii=False), flush=True)
        result['requests'].sort(key=lambda entry: entry['index'])
        durations = [entry['elapsed_seconds'] for entry in result['requests']]
        result.update(successful=sum(entry['success'] for entry in result['requests']),
                      wall_seconds=round(max(entry['finished_seconds'] for entry in result['requests']) -
                                         min(entry['started_seconds'] for entry in result['requests']), 3),
                      median_seconds=round(statistics.median(durations), 3), max_seconds=max(durations))
        result['images_per_minute'] = round(result['successful'] * 60 / result['wall_seconds'], 3)
        save()
        if result['successful'] != level:
            report['stopped_reason'] = '本档出现失败，停止增加并发；已发出的请求均已等待完成。'
            break
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    return 1 if report.get('stopped_reason') else 0


if __name__ == '__main__':
    raise SystemExit(main())
