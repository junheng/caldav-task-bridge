#!/bin/sh
# Apply a concrete reviewed preview while the old bridge is stopped.
set -eu
umask 077
DOCKER=/usr/local/bin/docker
COMPOSE=/usr/local/bin/docker-compose
ROOT=/volume1/docker/caldav-bridge
COMPOSE_FILE=/volume1/docker/radicale/docker-compose.yml
OLD_IMAGE=${1:?expected old image required}
NEW_IMAGE=${2:?new tested image required}
APPROVED_REVIEW=${3:-}
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$ROOT/ops/lifecycle-deployment-$STAMP"
PREVIEW="$ROOT/ops/lifecycle-preview.json"
run_docker() { sudo -n "$DOCKER" "$@"; }
run_compose() { sudo -n "$COMPOSE" "$@"; }
test "$(run_docker inspect caldav-bridge --format '{{.Config.Image}}')" = "$OLD_IMAGE"
test -s "$PREVIEW"
run_docker image inspect "$NEW_IMAGE" >/dev/null
python3 - "$COMPOSE_FILE" "$OLD_IMAGE" <<'PY'
from pathlib import Path
import sys
assert Path(sys.argv[1]).read_text().count(sys.argv[2]) == 1, 'Unexpected compose image'
PY
mkdir "$BACKUP"
cp -p "$COMPOSE_FILE" "$BACKUP/docker-compose.yml"
run_docker inspect caldav-bridge --format '{{.Image}}' > "$BACKUP/old-image-id.txt"
run_docker image inspect "$NEW_IMAGE" --format '{{.Id}}' > "$BACKUP/new-image-id.txt"
# This installation has one verified services_network attachment.
NETWORK=$(run_docker inspect caldav-bridge --format '{{range $name,$value := .NetworkSettings.Networks}}{{$name}}{{end}}')
test "$NETWORK" = services_network
run_docker stop caldav-bridge
cp -p "$ROOT/data/state.json" "$BACKUP/state.json"
on_failure() {
    result=$?
    trap - EXIT
    if [ "$result" != 0 ]; then
        run_docker stop caldav-bridge >/dev/null 2>&1 || true
        echo "Lifecycle maintenance failed; bridge stays stopped to prevent resurrection. Backup: $BACKUP" >&2
    fi
    exit "$result"
}
trap on_failure EXIT
set -- python main.py --apply-preview /app/ops/lifecycle-preview.json --backup-dir "/app/data/lifecycle-maintenance-$STAMP"
if [ -n "$APPROVED_REVIEW" ]; then
    set -- "$@" --approve-review "$APPROVED_REVIEW"
fi
run_docker run --rm --network "$NETWORK" --env-file "$ROOT/.env" \
    -v "$ROOT/data:/app/data" -v "$ROOT/ops:/app/ops:ro" \
    "$NEW_IMAGE" "$@" > "$BACKUP/receipts.json"
python3 - "$BACKUP/receipts.json" <<'PY'
import json, sys
report=json.load(open(sys.argv[1]))
assert not report['summary'].get('pending'), 'Unresolved migration entries; inspect receipts before restarting'
print(json.dumps({'migration':report['summary'],'data_backup':report['backup_dir']},ensure_ascii=False))
PY
python3 - "$COMPOSE_FILE" "$OLD_IMAGE" "$NEW_IMAGE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text()
assert text.count(sys.argv[2]) == 1, 'Unexpected compose image'
p.write_text(text.replace(sys.argv[2],sys.argv[3]))
PY
run_compose -f "$COMPOSE_FILE" up -d --no-deps caldav-bridge
run_docker inspect caldav-bridge --format 'image={{.Config.Image}} status={{.State.Status}}'
echo "Lifecycle maintenance applied. Deployment backup: $BACKUP"
