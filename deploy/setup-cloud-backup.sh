#!/usr/bin/env bash
# Wire the nightly backup up to Google Drive, and prove it before trusting it.
#
#   sudo deploy/setup-cloud-backup.sh            # check, wire up, verify
#   sudo deploy/setup-cloud-backup.sh --check    # just report where things stand
#
# The Google sign-in is NOT done here and cannot be: it needs a browser and the
# account's own password. This script does everything either side of it —
# installs rclone, tells you exactly what to answer, writes the setting, takes
# a real backup, and then proves the copy can be read back down again.
#
# WHY THE VERIFY STEP IS THE POINT
# ---------------------------------------------------------------------------
# "It uploaded" is not the same as "it is there and readable". An upload can be
# accepted and stored truncated; credentials can work for a write and fail on a
# read; a remote can be configured against the wrong folder and cheerfully
# accept everything into nowhere useful. None of that shows up until the
# morning the office server is gone.
#
# So this finishes by pulling the backup back DOWN and opening it with the
# app's own code — the same thing a real restore would do.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-gatepass}"
REMOTE="${REMOTE:-gdrive}"
FOLDER="${FOLDER:-gate-pass-backups}"
ACCOUNT="${ACCOUNT:-fanzartbot@gmail.com}"
ENV_FILE="$APP_DIR/.env"
MODE="${1:-}"

say()  { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
note() { printf "    %s\n" "$*"; }
warn() { printf "    \033[33m%s\033[0m\n" "$*"; }
die()  { printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
id -u "$APP_USER" >/dev/null 2>&1 || die "no such user: $APP_USER"

as_app() { sudo -u "$APP_USER" "$@"; }

# --------------------------------------------------------------- 1. rclone ---
say "rclone"
if command -v rclone >/dev/null; then
    note "already installed — $(rclone version | head -1)"
else
    # A plain if, not `[ ... ] && { ... } || { ... }`. That idiom runs the
    # third branch whenever the SECOND one returns non-zero, so a warn() that
    # ever failed would silently install packages in --check mode. Not a risk
    # worth carrying in a script that runs apt.
    if [ "$MODE" = "--check" ]; then
        warn "not installed"
    else
        note "installing..."
        apt-get install -y -qq rclone >/dev/null 2>&1 \
            || die "could not install rclone (try: apt update, then apt install rclone)"
        note "installed — $(rclone version | head -1)"
    fi
fi

# ------------------------------------------------------------- 2. the remote --
say "The Google Drive connection"
if as_app rclone listremotes 2>/dev/null | grep -qx "${REMOTE}:"; then
    note "a remote called '$REMOTE' already exists"
    if as_app rclone lsd "${REMOTE}:" >/dev/null 2>&1; then
        note "and it works"
    else
        warn "but it does not answer — the token may have expired"
        warn "re-authorise with:  sudo -u $APP_USER rclone config reconnect ${REMOTE}:"
        [ "$MODE" = "--check" ] || die "fix the remote, then run this again"
    fi
else
    cat <<SETUP

    There is no '$REMOTE' remote yet, and this script cannot create it for you:
    it needs a browser and the Google password, which are yours, not mine.

    Run this, and answer as shown:

      sudo -u $APP_USER rclone config

        n                        New remote
        name>  $REMOTE
        Storage>  drive          (Google Drive — type 'drive')
        client_id>               leave BLANK, press Enter
        client_secret>           leave BLANK, press Enter
        scope>  1                (full access — it has to write)
        service_account_file>    leave BLANK, press Enter
        Edit advanced config?  n
        Use web browser to automatically authenticate?  n     <-- THIS ONE

    That last answer is the one people get wrong. This server has no browser.
    Answering 'n' makes rclone print a command like

        rclone authorize "drive" "....."

    Run THAT on your laptop. A browser opens; sign in as

        $ACCOUNT

    and approve. It prints a long token — paste it back into the server.

        Configure this as a Shared Drive?  n
        y) Yes this is OK

    Then run this script again and it will do the rest.

SETUP
    exit 1
fi

[ "$MODE" = "--check" ] || as_app rclone mkdir "${REMOTE}:${FOLDER}" 2>/dev/null

# --------------------------------------------------------------- 3. setting ---
say "The backup setting"
TARGET="rclone:${REMOTE}:${FOLDER}"
if grep -q "^GATE_PASS_BACKUP_MIRRORS=" "$ENV_FILE" 2>/dev/null; then
    note "already set: $(grep '^GATE_PASS_BACKUP_MIRRORS=' "$ENV_FILE")"
elif [ "$MODE" = "--check" ]; then
    warn "not set in $ENV_FILE"
else
    touch "$ENV_FILE"
    printf '\n# Off-machine backup, added by deploy/setup-cloud-backup.sh\nGATE_PASS_BACKUP_MIRRORS="%s"\n' \
        "$TARGET" >> "$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    note "added GATE_PASS_BACKUP_MIRRORS=\"$TARGET\""
fi

if [ "$MODE" = "--check" ]; then
    say "Check only — nothing was changed"
    exit 0
fi

# ---------------------------------------------------------------- 4. prove ----
say "Taking a real backup and mirroring it"
as_app "$APP_DIR/deploy/backup.sh" || die "the backup did not complete cleanly — see above"

say "Proving the copy can be read back down"
# Not the local file: the whole point is the copy that survives losing this
# machine, so the drill pulls from Drive and opens what it finds there.
as_app "$APP_DIR/deploy/restore.sh" --drill cloud \
    || die "the cloud copy could not be restored — it is NOT a working backup"

cat <<DONE

    ------------------------------------------------------------------
    Done, and verified end to end.

    Every night at 21:00 the register is backed up and copied to
    $ACCOUNT's Drive, under $FOLDER. The copy is read back and
    checked byte for byte; if it ever fails, the log says so:

      sudo tail -20 /var/log/gate-pass-backup.log

    To see what is up there, from this machine or any other with the
    same rclone config:

      sudo -u $APP_USER $APP_DIR/deploy/restore.sh --list

    To prove again, any time, that the cloud copy still works:

      sudo -u $APP_USER $APP_DIR/deploy/restore.sh --drill cloud

    Worth doing: put two-factor on $ACCOUNT. That Drive now holds
    the whole gate pass register, including the accounts table.
    ------------------------------------------------------------------

DONE
