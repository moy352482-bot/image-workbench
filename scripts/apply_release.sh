#!/usr/bin/env bash
# Linux/systemd updater. Only code releases and the current symlink are changed.
set -Eeuo pipefail
umask 022
archive=${1:?archive required}
expected_sha=${2:?sha256 required}
release_id=${3:?release id required}
root=/opt/image-workbench
service=image-workbench.service
[[ $EUID -eq 0 ]] || { echo 'Run through root or sudo.' >&2; exit 1; }
[[ $expected_sha =~ ^[0-9a-f]{64}$ && $release_id =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]] || exit 1
printf '%s  %s\n' "$expected_sha" "$archive" | sha256sum -c -
[[ -d "$root" && -f /etc/image-workbench.env ]] || { echo 'Server initial setup is required.' >&2; exit 1; }
[[ $(readlink -f "$root") == "$root" && ! -L "$root/releases" ]] || { echo 'Unexpected release root symlink.' >&2; exit 1; }
[[ $(systemctl show "$service" -p WorkingDirectory --value) == "$root/current" ]] || {
    echo 'Install the current-release systemd template before deploying.' >&2; exit 1;
}
case $(systemctl show "$service" -p ExecStart --value) in
    *"$root/current/.venv/bin/python"*) ;;
    *) echo 'Unexpected systemd executable; refusing deployment.' >&2; exit 1 ;;
esac
exec 9>"$root/.deploy.lock"
flock -n 9 || { echo 'Another deployment is in progress.' >&2; exit 1; }
[[ ! -e "$root/current" || -L "$root/current" ]] || { echo 'current must be a symlink.' >&2; exit 1; }
previous=''
if [[ -L "$root/current" ]]; then
    previous=$(readlink -f "$root/current")
    [[ -d "$previous" && "$previous" == "$root/releases/"* ]] || { echo 'Invalid previous release.' >&2; exit 1; }
fi
release="$root/releases/$release_id"
[[ ! -e "$release" ]] || { echo 'Release already exists.' >&2; exit 1; }
mkdir -p "$root/releases"
mkdir "$release"
python3 - "$archive" "$release" "$release_id" <<'PY'
import hashlib, json, pathlib, sys, tarfile
archive, destination, release_id = sys.argv[1:]
root = pathlib.Path(destination)
with tarfile.open(archive, 'r:gz') as bundle:
    members = bundle.getmembers()
    seen = set()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if (not member.isfile() or path.is_absolute() or '..' in path.parts or '\\' in member.name
                or member.name in seen or not (member.name in {'requirements.txt', 'requirements-web.txt', 'RELEASE.json'}
                or path.parts[0] == 'image_workbench'
                or member.name in {'skills/image-generation-default/SKILL.md', 'skills/image-generation-default/styles.json',
                                   'skills/image-generation-default/references/sources.md'}
                or member.name in {'config/script_models.json', 'config/script_pricing.json', 'config/script_routing.json', 'config/storyboard_pricing.json', 'config/h3_workflow.json'}
                or member.name in {'config/deepseek/' + name for name in (
                    'script_models.json', 'script_pricing.json', 'script_routing.json', 'storyboard_pricing.json')}
                or member.name in {'skills/seedance-2-video/SKILL.md', 'skills/seedance-2-video/SOURCES.md'}
                or member.name in {'skills/storyboard-planning/' + name for name in (
                    'SKILL.md', 'references/shot-design.md', 'references/continuity.md', 'references/SOURCES.json',
                    'NOTICE.txt', 'licenses/drama-skills.txt', 'licenses/vimax.txt', 'licenses/toonflow.txt', 'licenses/minimax-h3.txt')}
                or member.name in {'skills/h3-video-workflow/' + name for name in (
                    'SKILL.md', 'references/base-en.txt', 'references/ref-en.txt', 'references/SOURCES.json', 'LICENSE', 'NOTICE')}
                or member.name == 'config/prompt_enhancement.json'
                or (path.parts[:3] == ('skills', 'prompt-enhancement', 'skills') and path.suffix == '.json')
                or (path.parts[:3] == ('skills', 'image-asset-generation', 'styles') and path.suffix == '.json')
                or (path.parts[:2] == ('skills', 'video-script-generation') and path.suffix in {'.json', '.txt', '.md'}))):
            raise ValueError('Unexpected release member')
        seen.add(member.name)
    for member in members:
        target = root / member.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(bundle.extractfile(member).read())
manifest = json.loads((root / 'RELEASE.json').read_text())
assert manifest['release_id'] == release_id
assert set(manifest['files']) == seen - {'RELEASE.json'}
for name, expected in manifest['files'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
PY
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install --disable-pip-version-check -r "$release/requirements-web.txt"
(
    cd "$release"
    "$release/.venv/bin/python" -c 'import json; from pathlib import Path; from image_workbench.cli import main; from image_workbench.web import create_app; from image_workbench.script_templates import ScriptTemplateLoader; from image_workbench.script_models import ScriptModelRegistry; from image_workbench.storyboard_planner import build_prompt; from image_workbench.image_prompting import default_image_skill; from image_workbench.prompt_enhancement import PromptEnhancementService; loader=ScriptTemplateLoader(); assert loader.list_templates(); assert all(loader.load_skill(kind)["instructions"] for kind in ("script", "screenplay")); ScriptModelRegistry().configs(); assert build_prompt({"text": "release smoke", "artifact_id": "smoke", "version_id": "v1"}, {}, {}); assert isinstance(json.loads(Path("config/storyboard_pricing.json").read_text())["models"], dict); assert PromptEnhancementService.from_config().skills.list(); assert default_image_skill().catalog()["styles"]; assert default_image_skill().compile("release smoke")["compiled_prompt"]'
)

health() {
    local expected_release=$1
    local pid
    for ((attempt=0; attempt<20; attempt++)); do
        pid=$(systemctl show "$service" -p MainPID --value)
        if [[ $pid =~ ^[1-9][0-9]*$ && $(readlink -f "/proc/$pid/cwd") == "$expected_release" ]] && systemctl is-active --quiet "$service"; then
            if "$expected_release/.venv/bin/python" - "$pid" <<'PY'
import json, pathlib, sys, urllib.request
from urllib.parse import urlsplit
environ = dict(item.split(b'=', 1) for item in pathlib.Path('/proc/' + sys.argv[1] + '/environ').read_bytes().split(b'\0') if b'=' in item)
origin = environ[b'IMAGE_WORKBENCH_PUBLIC_ORIGIN'].decode()
request = urllib.request.Request('http://127.0.0.1:7860/api/auth/config', headers={'Host': urlsplit(origin).netloc})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
    payload = json.loads(response.read(8192))
    assert response.status == 200 and isinstance(payload.get('csrf_token'), str) and 'signed_in' in payload
PY
            then return 0; fi
        fi
        sleep 1
    done
    return 1
}

switch_link() {
    ln -s "$1" "$root/.switch-$release_id"
    mv -Tf "$root/.switch-$release_id" "$root/current"
}

rollback() {
    local status=$?
    (( status != 0 )) || status=1
    trap - ERR HUP INT TERM
    echo 'New release failed; attempting code rollback.' >&2
    if [[ -n "$previous" ]]; then
        switch_link "$previous"
        if systemctl restart "$service" && health "$previous"; then
            echo "Previous release restored: $previous" >&2
        else
            echo 'Rollback requires attention: inspect systemctl status and journalctl.' >&2
        fi
    else
        systemctl stop "$service" || true
        echo 'No previous release exists; failed first release retained for diagnosis.' >&2
    fi
    exit "${status:-1}"
}
trap rollback ERR HUP INT TERM
switch_link "$release"
systemctl restart "$service"
health "$release"
trap - ERR HUP INT TERM
echo "DEPLOYED $release_id"
echo 'Business data and server credentials were not copied or replaced.'
