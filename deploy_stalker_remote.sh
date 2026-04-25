#!/bin/bash
# Deploy ninja_stalker on a Linux host (always-on VPS or alphamind server).
# Run this script ONCE on the target host as root or with sudo. It:
#   1. Installs python3.13 (or latest python3) + pip + git + docker (optional)
#   2. Clones the bittensor coldkey from a backup tarball you upload
#   3. Clones taominer20/ninja
#   4. Installs python deps (bittensor)
#   5. Writes ~/.ninja_env with the BT password
#   6. Installs the cron entry
#
# Prerequisites on the LOCAL Mac before running this:
#   tar -czf /tmp/wallet_backup.tar.gz -C ~/.bittensor wallets
#   scp /tmp/wallet_backup.tar.gz user@host:/root/
#
# Then on the REMOTE host:
#   sudo bash deploy_stalker_remote.sh
#
# Note: this script needs sudo / root because it installs packages and
# manages user-level cron. Reads BT_PW interactively (does NOT take it
# on the command line — that would write it to bash history).

set -eu

NINJA_HOME="${NINJA_HOME:-/opt/ninja-stalker}"
NINJA_USER="${NINJA_USER:-$(whoami)}"
NINJA_REPO="${NINJA_REPO:-https://github.com/taominer20/ninja.git}"
WALLET_TARBALL="${WALLET_TARBALL:-/root/wallet_backup.tar.gz}"

echo "=== Ninja stalker remote deploy ==="
echo "  install dir : $NINJA_HOME"
echo "  user        : $NINJA_USER"
echo "  repo        : $NINJA_REPO"
echo "  wallet tar  : $WALLET_TARBALL"
echo

# 1. Install system packages
if command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y python3 python3-pip python3-venv git curl
  if ! command -v docker >/dev/null; then
    echo "Note: docker is NOT required on the stalker host; the stalker only"
    echo "      polls the dashboard, clones repos, applies toolbox, and pushes."
  fi
fi

# 2. Restore bittensor wallet
if [ -f "$WALLET_TARBALL" ]; then
  echo "Restoring wallet from $WALLET_TARBALL..."
  mkdir -p "$HOME/.bittensor"
  tar -xzf "$WALLET_TARBALL" -C "$HOME/.bittensor"
  ls "$HOME/.bittensor/wallets/" || true
else
  echo "WARN: wallet tarball $WALLET_TARBALL not found; create it on your Mac:"
  echo "  tar -czf /tmp/wallet_backup.tar.gz -C ~/.bittensor wallets"
  echo "Then scp it to this host before running this script again."
  exit 1
fi

# 3. Clone ninja repo
mkdir -p "$NINJA_HOME"
if [ ! -d "$NINJA_HOME/.git" ]; then
  git clone "$NINJA_REPO" "$NINJA_HOME"
else
  cd "$NINJA_HOME" && git pull --ff-only
fi

# 4. Python deps
pip3 install --quiet --upgrade bittensor

# 5. Env file
ENV_FILE="$HOME/.ninja_env"
if [ ! -f "$ENV_FILE" ]; then
  read -s -p "Bittensor coldkey password: " pw
  echo
  umask 077
  cat > "$ENV_FILE" <<EOF
export BT_PW='$pw'
export BT_PW_miner='$pw'
EOF
  chmod 600 "$ENV_FILE"
  echo "Wrote $ENV_FILE (mode 600)."
else
  echo "$ENV_FILE already exists; leaving as-is."
fi

# 6. Initial state seed
cd "$NINJA_HOME"
if [ ! -f .stalker_state.json ]; then
  python3 ninja_stalker.py --init
fi

# 7. Cron entry
PYTHON_BIN="$(command -v python3.13 || command -v python3)"
CRON_LINE="*/10 * * * * [ -f $ENV_FILE ] && . $ENV_FILE; cd $NINJA_HOME && $PYTHON_BIN ninja_stalker.py --once >>.stalker.cron.log 2>&1 # ninja-stalker"
( crontab -l 2>/dev/null | grep -v '# ninja-stalker'; echo "$CRON_LINE" ) | crontab -
echo "Cron installed:"
crontab -l | grep ninja-stalker

# 8. Verify
echo
echo "=== Smoke run ==="
. "$ENV_FILE"
$PYTHON_BIN "$NINJA_HOME/ninja_stalker.py" --once
echo
echo "=== Status ==="
$PYTHON_BIN "$NINJA_HOME/ninja_stalker.py" --status

echo
echo "Deploy complete. Stalker will run every 10 minutes via cron."
echo "Logs: $NINJA_HOME/.stalker.log + $NINJA_HOME/.stalker.cron.log"
