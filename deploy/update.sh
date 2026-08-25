#!/usr/bin/env bash
# Pull, fix up, restart. The supported way to update a running VPS.
#
#   sudo bash /opt/trader/deploy/update.sh
#
# Exists because doing this by hand goes wrong in a specific, repeatable way:
# git runs as root, so every file it rewrites ends up root-owned, and the
# service user then cannot write the files it owns. That failure shows up much
# later as a permission error on state.json rather than at the pull, so it is
# worth having one command that always does the whole sequence.

set -euo pipefail

DIR="${DIR:-/opt/trader}"
USER_NAME="${USER_NAME:-trader}"
UNIT=/etc/systemd/system/trader.service

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash deploy/update.sh)" >&2
  exit 1
fi

git config --global --add safe.directory "$DIR" 2>/dev/null || true

echo "==> pulling"
before=$(git -C "$DIR" rev-parse --short HEAD)
# The service commits state.json, so the tree is often dirty here. Stash it
# rather than fail: that file is regenerated on the next tick anyway.
git -C "$DIR" stash push --quiet -- site/data 2>/dev/null || true
git -C "$DIR" pull --ff-only
after=$(git -C "$DIR" rev-parse --short HEAD)
echo "    $before -> $after"

echo "==> dependencies"
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo "==> tests"
# A box that cannot pass its own risk tests should not resume trading.
( cd "$DIR" && "$DIR/.venv/bin/python" -m pytest tests/ -q )

echo "==> ownership"
# Everything git just touched is root-owned. Hand it all back.
chown -R "$USER_NAME:$USER_NAME" "$DIR"
chmod 600 "$DIR/.env" 2>/dev/null || true
[[ -f "$DIR/.ssh/deploy_ed25519" ]] && chmod 600 "$DIR/.ssh/deploy_ed25519"

echo "==> .env"
# install.sh only seeds .env when it does not exist, so a key added to
# .env.example after the first install never reaches an existing box. Nothing
# reports that: the setting is simply absent and whatever needed it fails
# later, somewhere unrelated. Compare the key names - never the values.
if [[ -f "$DIR/.env" && -f "$DIR/.env.example" ]]; then
  missing=$(comm -23 \
    <(grep -oE '^[A-Z_]+=' "$DIR/.env.example" | sort -u) \
    <(grep -oE '^[A-Z_]+=' "$DIR/.env" | sort -u))
  if [[ -n "$missing" ]]; then
    echo "    new settings in .env.example that your .env does not have:"
    echo "$missing" | sed 's/=$//; s/^/      /'
    echo "    add them with: nano $DIR/.env"
  else
    echo "    up to date with .env.example"
  fi
fi

echo "==> unit"
if ! cmp -s "$DIR/deploy/trader.service" "$UNIT"; then
  cp "$DIR/deploy/trader.service" "$UNIT"
  systemctl daemon-reload
  echo "    unit file updated"
fi

echo "==> restart"
# Marked before the restart so the log below can be scoped to this run only.
# `journalctl -n 15` shows the last fifteen lines whatever their age, which
# right after a fix means proudly displaying the errors it just fixed.
restarted_at=$(date -u +"%Y-%m-%d %H:%M:%S")
systemctl restart trader
sleep 6
systemctl is-active trader

echo "==> log since this restart"
journalctl -u trader --since "$restarted_at" --no-pager --output=cat | grep -v "^$" || true

if journalctl -u trader --since "$restarted_at" --no-pager \
     | grep -qiE "tick failed|publish failed|Traceback"; then
  echo
  echo "!! errors since restart - see above" >&2
  exit 1
fi
echo "    no errors since restart"
