#!/usr/bin/env bash
# One-shot VPS setup. Run as root on a fresh Debian/Ubuntu box.
#
#   curl -fsSL https://raw.githubusercontent.com/LordOftheIdiot5/trader/main/deploy/install.sh | bash
#
# or, having cloned already:  sudo bash deploy/install.sh
#
# Creates an unprivileged user, clones the repo, builds the venv, and installs
# the systemd unit. It deliberately does NOT start the service: .env has to be
# filled in first, and starting without it would just crash-loop.

set -euo pipefail

REPO="${REPO:-https://github.com/LordOftheIdiot5/trader.git}"
DIR="${DIR:-/opt/trader}"
USER_NAME="${USER_NAME:-trader}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash deploy/install.sh)" >&2
  exit 1
fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> user $USER_NAME"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  # No login shell and no home: this account exists to run one process.
  useradd --system --shell /usr/sbin/nologin --home-dir "$DIR" "$USER_NAME"
fi

echo "==> code at $DIR"
# Marked trusted before any git call: the clone below chowns the tree to the
# service user, and every later root-run git command would otherwise stop
# with "detected dubious ownership".
git config --global --add safe.directory "$DIR" 2>/dev/null || true
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi

echo "==> venv"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo "==> tests"
# If the risk gate does not pass here, nothing should be trading on this box.
( cd "$DIR" && "$DIR/.venv/bin/python" -m pytest tests/ -q )

mkdir -p "$DIR/var" "$DIR/site/data"
if [[ ! -f "$DIR/.env" ]]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "==> wrote $DIR/.env from the example - fill it in before starting"
fi
# The credentials file is readable by its owner and nobody else.
chmod 600 "$DIR/.env"
chown -R "$USER_NAME:$USER_NAME" "$DIR"

echo "==> systemd"
cp "$DIR/deploy/trader.service" /etc/systemd/system/trader.service
systemctl daemon-reload

cat <<EOF

Installed, not started.

  1. Edit $DIR/.env          (read the comments: trade-only keys, no withdrawal)
  2. Check $DIR/config.yaml  (mode: paper to start)
  3. sudo systemctl enable --now trader
  4. journalctl -u trader -f

Stop all trading at any time:

  sudo -u $USER_NAME touch $DIR/var/HALT

EOF
