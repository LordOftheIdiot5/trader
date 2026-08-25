#!/usr/bin/env bash
# Gives the VPS write access to this one repo, so the engine can publish
# state.json and the dashboard stays current.
#
#   sudo bash deploy/setup-push.sh
#
# Generates a repo-scoped deploy key and prints the public half. Add that at
# Settings -> Deploy keys -> Add deploy key, with "Allow write access" ticked.
# Scoped to this repository alone: if the VPS is ever compromised, the key
# cannot touch anything else you own.
#
# Safe to re-run. An existing key is reused rather than replaced, because
# replacing it would silently break pushing until the new key was added.

set -euo pipefail

DIR="${DIR:-/opt/trader}"
USER_NAME="${USER_NAME:-trader}"
KEY="$DIR/.ssh/deploy_ed25519"
REPO_SSH="git@github.com:LordOftheIdiot5/trader.git"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo bash deploy/setup-push.sh)" >&2
  exit 1
fi

# The repo is owned by the service user but these commands run as root, so
# git refuses to touch it until the path is marked trusted. Root already has
# full access; this only silences a guard aimed at multi-user machines.
git config --global --add safe.directory "$DIR" 2>/dev/null || true

mkdir -p "$DIR/.ssh"

if [[ -f "$KEY" ]]; then
  echo "==> reusing existing deploy key"
else
  echo "==> generating deploy key"
  ssh-keygen -q -t ed25519 -f "$KEY" -N '' -C "trader-vps-deploy"
fi

# Pin the host key now so the first push is not an interactive prompt in a
# systemd unit that has no terminal to prompt on.
touch "$DIR/.ssh/known_hosts"
ssh-keyscan -t ed25519 github.com 2>/dev/null | grep -q github.com \
  && ssh-keyscan -t ed25519 github.com 2>/dev/null >> "$DIR/.ssh/known_hosts"
sort -u "$DIR/.ssh/known_hosts" -o "$DIR/.ssh/known_hosts"

echo "==> pointing the repo at SSH and this key"
git -C "$DIR" remote set-url origin "$REPO_SSH"
git -C "$DIR" config core.sshCommand \
  "ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$DIR/.ssh/known_hosts"
# Commits from the VPS should be attributable to the machine, not to a person.
git -C "$DIR" config user.name "trader-vps"
git -C "$DIR" config user.email "trader-vps@users.noreply.github.com"

# Whole tree, not just the two directories touched above: anything git has
# rewritten while running as root is now root-owned, and the service user
# has to be able to write every file it is responsible for.
chown -R "$USER_NAME:$USER_NAME" "$DIR"
chmod 700 "$DIR/.ssh"
chmod 600 "$KEY"

cat <<EOF

==> Add this as a deploy key with WRITE access:
    https://github.com/LordOftheIdiot5/trader/settings/keys/new

EOF
cat "$KEY.pub"
cat <<EOF

Then verify from here:

  sudo -u $USER_NAME git -C $DIR push

EOF
