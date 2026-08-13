#!/usr/bin/env bash
# Pull the latest code on the VM and make it live.
#
#     ./scripts/deploy.sh
#
# Only does the work the changes actually require: the UI is rebuilt when
# ui/ changed, and the Python services restart when their code changed. A UI
# tweak therefore does not bounce the API, and an API fix does not sit through
# an npm build.
#
# Safe to run when nothing changed - it says so and exits.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$(pwd)
WEB_ROOT=/srv/naijafares/web          # must match root* in infra/caddy/Caddyfile
VENV="$REPO/.venv"

say() { printf '\n==> %s\n' "$1"; }

# --- 1. pull ----------------------------------------------------------------
before=$(git rev-parse HEAD)
say "Pulling"
# --ff-only: refuse to create a merge commit on a server. If this fails, the VM
# has local edits, and silently merging them is how a box drifts from the repo.
git pull --ff-only
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    echo "    Already up to date - nothing to deploy."
    exit 0
fi

changed=$(git diff --name-only "$before" "$after")
echo "    $(echo "$changed" | wc -l) file(s) changed"
changed_in() { echo "$changed" | grep -qE "$1"; }

# --- 2. dependencies --------------------------------------------------------
if changed_in '^requirements.*\.txt$'; then
    say "Python dependencies changed - installing"
    "$VENV/bin/pip" install -q -r requirements.txt
    [ -f requirements-ml.txt ] && "$VENV/bin/pip" install -q -r requirements-ml.txt
fi

# --- 3. the UI --------------------------------------------------------------
if changed_in '^ui/'; then
    say "UI changed - rebuilding"
    cd "$REPO/ui"
    # npm ci only when the lockfile moved; it is far slower than a build.
    changed_in '^ui/package(-lock)?\.json$' && npm ci || true
    npm run build

    # rm -rf first: Vite fingerprints filenames, so without it every deploy
    # leaves the previous build's assets behind for ever.
    say "Publishing to $WEB_ROOT"
    sudo rm -rf "$WEB_ROOT"
    sudo mkdir -p "$WEB_ROOT"
    sudo cp -r dist/. "$WEB_ROOT/"
    cd "$REPO"
fi

# --- 4. the services --------------------------------------------------------
restart=""
changed_in '^(api|common|booking|ml)/' && restart="$restart naijafares-api"
changed_in '^(simulator|common)/'      && restart="$restart naijafares-simulator"
changed_in '^dashboard/'               && restart="$restart naijafares-dashboard"

if [ -n "$restart" ]; then
    say "Restarting:$restart"
    # shellcheck disable=SC2086
    sudo systemctl restart $restart
fi

# --- 5. infrastructure ------------------------------------------------------
# These are NOT applied automatically: copying a unit file or a Caddyfile can
# take the site down, and doing that as a side effect of a UI tweak would be
# a nasty surprise. Say so and let a human decide.
if changed_in '^infra/systemd/'; then
    say "NOTE: systemd units changed. To apply:"
    echo "    sudo cp infra/systemd/*.service /etc/systemd/system/"
    echo "    sudo sed -i \"s/^User=.*/User=\$USER/\" /etc/systemd/system/naijafares-*.service"
    echo "    sudo systemctl daemon-reload && sudo systemctl restart naijafares-*"
fi
if changed_in '^infra/caddy/'; then
    say "NOTE: Caddyfile changed. To apply:"
    echo "    sudo cp infra/caddy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy"
fi
if changed_in '^(docker-compose\.yml|stream/|infra/connect/)'; then
    say "NOTE: the stack or stream layer changed. To apply:"
    echo "    ./scripts/bootstrap.sh"
fi

# --- 6. did it work? --------------------------------------------------------
say "Checking"
sleep 3
systemctl is-active naijafares-api naijafares-simulator naijafares-dashboard \
    | paste -d' ' - - - | sed 's/^/    services: /'

FQDN=$(sudo awk -F= '/^SITE_ADDRESS=/{print $2}' /etc/caddy/caddy.env 2>/dev/null || true)
if [ -n "$FQDN" ]; then
    printf '    api:      '; curl -s --max-time 10 "https://$FQDN/api/health" || echo "NO RESPONSE"
    printf '\n    ui:       '; curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 "https://$FQDN/"
fi

say "Deployed $(git rev-parse --short HEAD)"
