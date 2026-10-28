# 0011. Online rooms, team scoring, and self-hosting

- Status: accepted
- Date: 2026-10-28

## Context
The owner wants Fare Dodger on their home server: Ubuntu, Docker Compose, Cloudflare Tunnel,
with port 8000 already taken. It should be playable with friends online, which they described
as follows:
- the start menu lets you create or join a game;
- the creator picks 1v1, 2v2 or 3v3 and starts the match;
- empty places are bots, and the hunting side always gets two extra bots;
- who starts on which side is random;
- every Dodger counts on their own: the catchers win by catching everyone, the runners by all
  escaping, and one of each makes the round 1-1.

## Decision
- **Rounds with seats** (`LiveGame`): a round takes two lists of seats, one per team, each a
  human player id or a bot. The hunting side gets `hunter_extra` (2) more bots.
  - Dodger seats: a human, or a `BotDodger` playing that runner.
  - Hunter seats: a human Bounty Hunter, or a bot in `BountyHunters`.
  - Every human has their own keys, messages and view (`snapshot(pid)`).
  - Dodgers are caught one at a time, and caught players watch their team. The round ends
    when every Dodger is caught, or when time runs out.
  - The reports cover every Dodger still free. Bot hunters share themselves out over the
    latest reports and chase the nearest Dodger in sight.
  - A player who leaves is replaced: their Dodger becomes a bot, and a Bounty Hunter stands
    still.
  - Without seats, a round is the single-player round as before, so the existing tests and
    tools keep working.
- **Scoring** (`Match`):
  - Every Dodger is one point: escaped for the runners, caught for the hunters.
  - The match goes to the first team past half the regulation points:
    `regulation_rounds × size / 2 + 1`, which is 6 in 1v1 and 11 in 2v2.
  - Sides switch at halftime. After a tie, they switch every round until one team leads by
    two points.
  - The view is per player: "you" are your team, "them" the other.
- **Rooms** (`server.py`):
  - **Connecting:** a WebSocket says `create` (with a size), `join` (with a 4-letter code) or
    `solo`.
  - **Lobby:** joiners go to the smaller team (randomly on a tie) and can switch. Only the
    host can start the match, and a random team runs first. One task per room steps the
    round and sends every player their snapshot.
  - **Between rounds:** the next round follows after an 8-second break, which the host can
    skip.
  - **After the match:** the host can take the room back to the lobby.
  - **Leaving:** a host who leaves is replaced by the next player, and a room with no players
    closes.
- **Caps** (environment variables):
  - `FARE_DODGER_MAX_MATCHES` (20 rooms);
  - `FARE_DODGER_MAX_MATCHES_PER_IP` (2 rooms per address, from `CF-Connecting-IP` when
    present, otherwise the socket's peer, which behind the tunnel is cloudflared);
  - `FARE_DODGER_IDLE_S` (120 s without input closes the room).
- **Service date:** `"auto"` now picks inside the feed's validity (`feed_info.txt`
  start/end dates), and a pinned date outside it is refused with the valid range. The live
  config uses `"auto"`, so the server keeps working when BKK publishes a new feed.
  `configs/default.toml` stays pinned, because `docs/results.md` depends on that exact day.
- **Self-hosting:**
  - **Dockerfile:** two stages. uv 0.12.17 installs the locked dependencies and the project
    into a virtualenv; the runtime stage is `python:3.12-slim-bookworm` running as an
    unprivileged user.
  - **Start command:** refresh the feed, then `fare-dodger play` on port 8001.
  - **`compose.yaml`:** the `game` service with `./data` as a volume, the `/healthz`
    healthcheck (10-minute start period), `restart: unless-stopped` and a memory limit. A
    `tunnel` service (`cloudflare/cloudflared:2026.10.5`, token from `.env`) sits in the
    `tunnel` profile and waits for the game to be healthy.
  - **Memory**, measured on a cold first start: the process peaks at 859 MiB while building
    the networks, and serves at about 230 MiB. The limit defaults to 1.5 GiB.
- **Vendored front end:** MapLibre GL JS 4.7.1, three.js 0.169.0 (with the two add-ons used)
  and the Fredoka variable font (Latin and Latin Extended) are now static files, with their
  licences. The map tiles and glyphs still come from OpenFreeMap.

## Consequences
- **Bots fill every gap**, so a 3v3 room works with one human; it is just easier.
- **The hunting side is +2** in every room size, so a lone human Dodger in 3v3 faces five
  hunters. Retune `hunter_extra` per size if that proves too hard.
- **One Python process runs every room.** Per-room cost is small next to the shared networks.
  The caps keep a public server from being swamped.
- **The per-address cap is only meaningful behind Cloudflare**, or when players connect
  directly. Without `CF-Connecting-IP`, every tunnel user would share cloudflared's address,
  so put the game behind the tunnel only.
