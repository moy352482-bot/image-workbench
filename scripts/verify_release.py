"""Verify deployed runtime bytes without contacting the server or running a worker."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=root / 'releases/20261020T103920Z-8d69b7a2.json')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
    failures = []
    for relative, digest in manifest['files'].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            failures.append({'path': relative, 'reason': 'missing_or_unsafe'})
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            failures.append({'path': relative, 'reason': 'sha256_mismatch'})
    print(json.dumps({'release_id': manifest['release_id'], 'files': len(manifest['files']),
                      'failures': failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
