# 0014. Admin page and updating from it

- Status: accepted
- Date: 2026-10-29

## Context
The owner wants an admin page for their home server with an "Update now" button. Updating
means pulling the newest code from the private GitHub repository, rebuilding the image and
restarting the game. The container can't do that safely by itself:
- pulling needs the owner's git login, which lives on the host;
- rebuilding needs Docker on the host. Giving a web-facing container the Docker socket would
  make it root-equivalent on the host.

## Decision
- **Admin page:** `/admin`, plus the `admin.html` and `admin.js` static files. It is enabled
  by setting `FARE_DODGER_ADMIN_TOKEN`; every admin API call must send
  `Authorization: Bearer <token>`, compared in constant time. Without the variable, the admin
  API answers 404. It shows the running version and commit, uptime, memory, players, every
  room (code, size, players with teams, lobby or round and score, idle time) with a Close
  button, and the timetable's feed version and service day.
- **Update now** writes `data/admin/update-request` and does nothing else.
  - On the host, `deploy/install-updater.sh` installs a systemd path unit that watches that
    file, and a one-shot service. The service runs `deploy/update.sh` as the owner, in the
    repository.
  - The script runs `git pull --ff-only`, then `docker compose up -d --build
    --remove-orphans`, adding the `tunnel` profile when a tunnel token is configured. It
    passes the new commit as `FARE_DODGER_COMMIT`.
  - Progress and a log tail go to `data/admin/update-status.json`, which the admin page
    polls. A request left unhandled for a minute is reported as "is the updater installed?".
  - The game container never touches Docker or git.
- **Closing a game:** Close sets the room's close reason, so its players get "The admin
  closed this game." and go back to the menu. The idle timeout uses the same path.

## Consequences
- An update restarts the game, so matches in progress are cut off; the page warns before
  updating. The restart also runs `fare-dodger download`, which refreshes the timetable.
- The admin password is the only check inside the game. Behind Cloudflare Access, a second
  Access application for `/admin` and `/api/admin` that allows only the owner's email adds a
  second lock.
- Running `deploy/update.sh` by hand does the same as the button.
