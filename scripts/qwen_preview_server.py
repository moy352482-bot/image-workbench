"""Isolated loopback acceptance server; never opens the production data directory."""
import json
from pathlib import Path
import secrets
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_workbench import StorageConfig, StorageManager
from image_workbench.remote_access import RemoteAccess
from image_workbench.web import create_app

runtime = ROOT / '.deploy/qwen-preview'
runtime.mkdir(parents=True, exist_ok=True)
path = runtime / 'credentials.json'
if not path.exists():
    path.write_text(json.dumps({'server': 'http://127.0.0.1:7863', 'worker_token': secrets.token_urlsafe(40),
                               'username': 'moy', 'password': secrets.token_urlsafe(24)}), encoding='utf-8')
credentials = json.loads(path.read_text(encoding='utf-8'))
storage = StorageManager(StorageConfig(data_root=runtime/'data'))
app = create_app(storage, backend='codex', remote_access=RemoteAccess(credentials['server'], credentials['worker_token']))
with app.identity.connect() as db:
    active = bool(db.execute("SELECT password_hash FROM accounts WHERE username='moy'").fetchone()[0])
if not active:
    activation = json.loads((runtime/'data/.identity/moy-activation.json').read_text(encoding='utf-8'))
    app.identity.activate(activation['activation_token'], credentials['password'])
uvicorn.run(app, host='127.0.0.1', port=7863, log_level='warning')
