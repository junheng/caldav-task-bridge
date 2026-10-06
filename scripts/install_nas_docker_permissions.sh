#!/bin/sh
# One-time installation, run by the NAS administrator with sudo.
set -eu

if [ "$(id -u)" != 0 ]; then
    echo 'Run this installer with sudo on the NAS.' >&2
    exit 1
fi

HOOK_DIR=/usr/local/etc/rc.d
HOOK="$HOOK_DIR/S99-caldav-bridge-permissions.sh"
mkdir -p "$HOOK_DIR"
TEMP_HOOK=$(mktemp "$HOOK_DIR/.caldav-bridge-permissions.XXXXXX")
trap 'rm -f "$TEMP_HOOK"' EXIT

cat > "$TEMP_HOOK" <<'BOOT_SCRIPT'
#!/bin/sh
set -eu
case "${1:-start}" in
    start)
        mkdir -p /etc/sudoers.d
        rule_file=$(mktemp /etc/sudoers.d/.caldav-bridge-maintenance.XXXXXX)
        trap 'rm -f "$rule_file"' EXIT
        printf '%s\n' 'gong.junheng ALL=(root) NOPASSWD: /usr/local/bin/docker *, /usr/local/bin/docker-compose *' > "$rule_file"
        chown root:root "$rule_file"
        chmod 0440 "$rule_file"
        mv -f "$rule_file" /etc/sudoers.d/caldav-bridge-maintenance
        ;;
    *) exit 0 ;;
esac
BOOT_SCRIPT

sh -n "$TEMP_HOOK"
chown root:root "$TEMP_HOOK"
chmod 0700 "$TEMP_HOOK"
mv -f "$TEMP_HOOK" "$HOOK"
"$HOOK" start

# Verify from the actual SSH account, rather than from root.
sudo -n -u gong.junheng /bin/sh -c 'sudo -n /usr/local/bin/docker ps --format "{{.Names}} {{.Status}}"'
echo 'Docker/Compose maintenance permissions installed; DSM boot hook will restore them at startup.'
