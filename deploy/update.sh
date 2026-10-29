#!/usr/bin/env bash
# Update Fare Dodger to the newest version on GitHub and restart it.
#
# Run by the admin page's "Update now" button (through the systemd units installed by
# deploy/install-updater.sh), or by hand: ./deploy/update.sh
# It pulls with your own git login, rebuilds the image and recreates the containers that
# changed; the game's data in ./data is kept. Progress goes to data/admin/update-status.json,
# which the admin page shows.
set -uo pipefail

cd "$(dirname "$0")/.."
admin_dir=data/admin
status=$admin_dir/update-status.json
log=$admin_dir/update.log
mkdir -p "$admin_dir"
rm -f "$admin_dir/update-request"

# Start the tunnel too if it is configured (a token in .env).
profiles=()
if grep -qE '^CLOUDFLARE_TUNNEL_TOKEN=.+' .env 2>/dev/null; then
  profiles=(--profile tunnel)
fi

write_status() {  # state, started, finished, before, after
  python3 - "$@" "$log" "$status" <<'PY'
import json, pathlib, sys
state, started, finished, before, after, log, out = sys.argv[1:8]
text = pathlib.Path(log).read_text(errors="replace") if pathlib.Path(log).is_file() else ""
data = {"state": state, "started": started, "finished": finished or None,
        "before": before, "after": after or None, "log": text[-6000:]}
tmp = pathlib.Path(out + ".part")
tmp.write_text(json.dumps(data))
tmp.replace(out)
PY
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
started=$(now)
before=$(git rev-parse --short HEAD)
: > "$log"
write_status running "$started" "" "$before" ""

{
  echo "== git pull"
  git pull --ff-only
  echo "== docker compose up --build"
  FARE_DODGER_COMMIT=$(git rev-parse --short HEAD) \
    docker compose "${profiles[@]}" up -d --build --remove-orphans
} >>"$log" 2>&1
result=$?

after=$(git rev-parse --short HEAD)
if [ "$result" -eq 0 ]; then state=done; else state=failed; fi
echo "== $state (exit $result)" >>"$log"
write_status "$state" "$started" "$(now)" "$before" "$after"
exit "$result"
