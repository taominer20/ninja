#!/bin/bash
# Install ninja_stalker as a cron job that runs every 10 minutes.
# Reads BT password from ~/.ninja_env (created by this script if missing).
#
# Usage:
#   ./install_stalker_cron.sh install    # add cron entry, prompt for BT_PW
#   ./install_stalker_cron.sh uninstall  # remove cron entry
#   ./install_stalker_cron.sh status     # show current cron + last run

set -u

NINJA_ROOT="/Users/alexanderlange/Desktop/ninja"
ENV_FILE="$HOME/.ninja_env"
CRON_TAG="# ninja-stalker"
CRON_ENTRY="*/10 * * * * cd $NINJA_ROOT && [ -f $ENV_FILE ] && . $ENV_FILE; /opt/homebrew/bin/python3.13 $NINJA_ROOT/ninja_stalker.py --once >>$NINJA_ROOT/.stalker.cron.log 2>&1 $CRON_TAG"

cmd="${1:-status}"

case "$cmd" in
  install)
    if [ ! -f "$ENV_FILE" ]; then
      echo "Creating $ENV_FILE (mode 600)."
      read -s -p "Bittensor coldkey password: " pw
      echo
      umask 077
      cat > "$ENV_FILE" <<EOF
# ninja stalker env — DO NOT commit, DO NOT chmod beyond 600
export BT_PW='$pw'
export BT_PW_miner='$pw'
EOF
      chmod 600 "$ENV_FILE"
      echo "wrote $ENV_FILE"
    else
      echo "$ENV_FILE already exists; leaving as-is"
      ls -la "$ENV_FILE"
    fi

    # Run init once to prime state file (so historical events don't all fire)
    if [ ! -f "$NINJA_ROOT/.stalker_state.json" ]; then
      echo "priming state via --init …"
      cd "$NINJA_ROOT"
      /opt/homebrew/bin/python3.13 ninja_stalker.py --init
    fi

    # Add to crontab if not already there
    current=$(crontab -l 2>/dev/null || true)
    if echo "$current" | grep -q "$CRON_TAG"; then
      echo "cron entry already present:"
      echo "$current" | grep "$CRON_TAG"
    else
      echo "$current" | grep -v "$CRON_TAG" > /tmp/_crontab_new
      echo "$CRON_ENTRY" >> /tmp/_crontab_new
      crontab /tmp/_crontab_new
      rm /tmp/_crontab_new
      echo "installed cron entry."
    fi

    echo
    echo "current cron:"
    crontab -l 2>/dev/null | grep "$CRON_TAG" || echo "(no entry found)"
    ;;
  uninstall)
    current=$(crontab -l 2>/dev/null || true)
    echo "$current" | grep -v "$CRON_TAG" | crontab -
    echo "removed ninja-stalker cron entries."
    ;;
  status)
    echo "cron:"
    crontab -l 2>/dev/null | grep "$CRON_TAG" || echo "  (no ninja-stalker cron entry)"
    echo
    echo "env file:"
    if [ -f "$ENV_FILE" ]; then
      ls -la "$ENV_FILE"
    else
      echo "  (no $ENV_FILE — run 'install' to create)"
    fi
    echo
    echo "stalker state:"
    if [ -f "$NINJA_ROOT/.stalker_state.json" ]; then
      cd "$NINJA_ROOT" && /opt/homebrew/bin/python3.13 ninja_stalker.py --status
    else
      echo "  (no state file yet)"
    fi
    echo
    echo "last 5 cron-run log lines:"
    if [ -f "$NINJA_ROOT/.stalker.cron.log" ]; then
      tail -5 "$NINJA_ROOT/.stalker.cron.log"
    else
      echo "  (no log yet)"
    fi
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}"
    exit 2
    ;;
esac
