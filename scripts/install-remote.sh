#!/bin/sh
# Install youchat on a remote host as the CURRENT ssh user (no root needed) and keep the hub
# alive with cron (works on hosts without `systemctl --user`, e.g. cluster login nodes).
#
#   ./scripts/install-remote.sh myserver
#
# Layout on the remote:
#   ~/.local/share/youchat/youchat.pyz   the zipapp
#   ~/.local/bin/youchat                 wrapper pinned to the newest python >= 3.8 found
#   ~/.youchat/                          DB + hub.log (0700)
#   /tmp/youchat-<uid>/hub.sock          hub socket on node-local disk (0700 dir / 0600 socket)
set -eu
HOST=${1:?usage: install-remote.sh <ssh-host>}
cd "$(dirname "$0")/.."
./scripts/build.sh
ssh "$HOST" 'mkdir -p ~/.local/share/youchat ~/.local/bin'
scp -q dist/youchat "$HOST":.local/share/youchat/youchat.pyz
ssh "$HOST" 'sh -s' <<'EOF'
set -eu
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
  p=$(command -v "$c" 2>/dev/null) || continue
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then PY=$p; break; fi
done
[ -n "$PY" ] || { echo "no python >= 3.8 found on remote" >&2; exit 1; }
cat > "$HOME/.local/bin/youchat" <<W
#!/bin/sh
exec $PY $HOME/.local/share/youchat/youchat.pyz "\$@"
W
chmod 755 "$HOME/.local/bin/youchat"
Y="$HOME/.local/bin/youchat"
# Keep the hub alive: '--daemon' is a no-op when it's already running.
( crontab -l 2>/dev/null | grep -v '# youchat-hub' || true
  echo "@reboot $Y hub --daemon >/dev/null 2>&1 # youchat-hub"
  echo "*/5 * * * * $Y hub --daemon >/dev/null 2>&1 # youchat-hub" ) | crontab -
# Restart the hub so a freshly installed version takes effect.
pkill -u "$(id -u)" -f 'youchat.pyz hub' 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -u "$(id -u)" -f 'youchat.pyz hub' >/dev/null || break; sleep 0.5; done
"$Y" hub --daemon
sleep 1
echo "python: $PY"
"$Y" --version
"$Y" status
EOF
