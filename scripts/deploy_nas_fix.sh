#!/bin/sh
# Run on the project's existing Synology NAS after installing Docker permissions.
set -eu
umask 077

DOCKER=/usr/local/bin/docker
COMPOSE=/usr/local/bin/docker-compose
COMPOSE_FILE=/volume1/docker/radicale/docker-compose.yml
BRIDGE_DIR=/volume1/docker/caldav-bridge
OLD_IMAGE=${1:-dkreg.sigmoid.cc:53691/caldav-task-bridge:0.1.8}
NEW_IMAGE=${2:-dkreg.sigmoid.cc:53691/caldav-task-bridge:ios-fix-20261006}
ARCHIVE="$BRIDGE_DIR/ops/caldav-task-bridge-${NEW_IMAGE##*:}.tar.gz"
BACKUP_DIR="$BRIDGE_DIR/ops/backup-$(date +%Y%m%d-%H%M%S)"

run_docker() {
    if [ "$(id -u)" = 0 ]; then
        "$DOCKER" "$@"
    else
        sudo -n "$DOCKER" "$@"
    fi
}

run_compose() {
    if [ "$(id -u)" = 0 ]; then
        "$COMPOSE" "$@"
    else
        sudo -n "$COMPOSE" "$@"
    fi
}

# Reject unexpected deployments before changing the running service.
test "$(run_docker inspect caldav-bridge --format '{{.Config.Image}}')" = "$OLD_IMAGE"
test -f "$COMPOSE_FILE"
test -f "$BRIDGE_DIR/data/state.json"
python3 - "$COMPOSE_FILE" "$OLD_IMAGE" <<'PY'
import sys
from pathlib import Path
assert Path(sys.argv[1]).read_text().count(sys.argv[2]) == 1, 'Unexpected compose image'
PY

if [ -f "$ARCHIVE" ]; then
    run_docker load -i "$ARCHIVE"
else
    run_docker pull "$NEW_IMAGE"
fi
run_docker image inspect "$NEW_IMAGE" >/dev/null
mkdir -p "$BACKUP_DIR"
cp -p "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.yml"
run_docker inspect caldav-bridge --format '{{.Image}}' > "$BACKUP_DIR/old-image-id.txt"

rollback() {
    result=$?
    trap - EXIT
    if [ "$result" != 0 ]; then
        echo "Deployment failed; restoring previous compose file. Backup: $BACKUP_DIR" >&2
        cp -p "$BACKUP_DIR/docker-compose.yml" "$COMPOSE_FILE"
        run_compose -f "$COMPOSE_FILE" up -d --no-deps caldav-bridge || true
    fi
    exit "$result"
}
trap rollback EXIT

run_docker stop caldav-bridge
cp -p "$BRIDGE_DIR/data/state.json" "$BACKUP_DIR/state.json"
python3 - "$COMPOSE_FILE" "$OLD_IMAGE" "$NEW_IMAGE" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
assert text.count(sys.argv[2]) == 1, 'Unexpected compose image'
path.write_text(text.replace(sys.argv[2], sys.argv[3]))
PY
run_compose -f "$COMPOSE_FILE" up -d --no-deps caldav-bridge
run_docker inspect caldav-bridge --format 'image={{.Config.Image}} status={{.State.Status}}'
echo "Deployment started. Backup: $BACKUP_DIR"
echo 'Verify successful push/pull and queue progress before declaring recovery.'
