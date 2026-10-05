# openSNAP

Open, clean-room implementation of the Sega Network Application Package (SN@P).

## Overview

openSNAP is designed with a strict separation between:

- protocol and transport core logic,
- state and storage logic,
- game-specific extensions.

This keeps core behavior reusable while allowing game integrations to be added independently.

## Project Status

Currently supported games:

PlayStation 2:
  - Auto Modellista Public Beta volume 1.0 (SLUS_204.98)
  - Auto Modellista Public Beta volume 2.0 (SLUS_280.31)
  - Auto Modellista NTSC-U/C (SLUS_206.42)
  - Monster Hunter NTSC-U (SLUS_208.96)
  - Monster Hunter PAL (SLES_527.07), with crossplay with NTSC-U

## SNAP History (Brief)

SN@P is the direct continuation of KAGE, an in-house online gaming middleware developed by SEGA primarily for Dreamcast titles. It later evolved into the **SEGA Network Application Package**.

In the early 2000s, SEGA positioned SNAP as a cross-platform networking stack for game developers. In **December 2002**, SEGA announced middleware agreements that made SNAP available to PlayStation 2 and GameCube developers.

On **August 19, 2003**, Nokia and SEGA announced an agreement for Nokia to acquire select Sega.com technology, including SNAP. Nokia stated that SNAP would become core technology for its online mobile gaming push (especially around N-Gage services).

After that transition, the platform was commonly referred to in Nokia's mobile ecosystem as **SNAP Mobile**, and industry coverage from that period described SNAP as **Scalable Network Application Package** in its Nokia-era mobile form.

Historical references:

- Nokia/SEGA transfer announcement (Aug 19, 2003): https://www.globenewswire.com/news-release/2003/08/19/1847054/0/en/Nokia-and-SEGA-reach-agreement-on-the-transfer-of-select-SEGA-com-leading-technology.html
- Sega middleware rollout coverage (Dec 4, 2002): https://www.gamedeveloper.com/game-platforms/sega-networking-middleware-rolls-out-to-ps2-gamecube-developers
- Nokia/Sun SNAP Mobile coverage (Jul 1, 2004): https://www.gamespot.com/articles/nokia-and-sun-bringing-snap-to-java-handsets/1100-6101766/

## Future Goals

Next planned protocol targets:

- Western versions of Resident Evil Outbreak

## Prerequisites

Install these first:

- `git`
- `python3` (3.11 or newer)
- `pip` (usually included with Python)

Optional but recommended checks:

```bash
git --version
python3 --version
python3 -m pip --version
```

## Install From Scratch

1. Clone the repository.

```bash
git clone https://github.com/GrenderG/openSNAP
cd openSNAP
```

2. Create and activate a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies.

```bash
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

4. Create local configuration file (optional).

```bash
cp .env.dist .env
```

Edit `.env` as needed for your machine. Local `.env` files are gitignored so local changes are not committed.
If `.env` is missing, openSNAP automatically creates it from `.env.dist` on first run.

## Run openSNAP (Game)

Start the SNAP game service:

```bash
python3 run.py game
```

Expected startup output includes:

```text
Starting openSNAP game server on 0.0.0.0:9091 using plugin <game-plugin>.
```

Stop the server with `Ctrl+C`.

`python3 run.py` without arguments is equivalent to `python3 run.py game`.

## Bootstrap And Game Server Configuration

Environment variables for the split UDP services:

- `OPENSNAP_BOOTSTRAP_HOST`: bootstrap bind host (default: `0.0.0.0`).
- `OPENSNAP_BOOTSTRAP_ADVERTISE_HOST`: optional IPv4 host advertised by the bootstrap service when it needs to emit its own endpoint. If empty, openSNAP derives it from `OPENSNAP_BOOTSTRAP_HOST` and client routing.
- `OPENSNAP_BOOTSTRAP_PORT`: bootstrap bind port (default: `9090`).
- `OPENSNAP_GAME_HOST`: game bind host (default: `0.0.0.0`).
- `OPENSNAP_GAME_ADVERTISE_HOST`: optional IPv4 host advertised to clients in bootstrap login-success packets. If empty, openSNAP derives it from `OPENSNAP_GAME_HOST` and client routing.
- `OPENSNAP_GAME_PORT`: game bind port (default: `9091`).
- `OPENSNAP_GAME_PLUGIN`: game plugin name (default: built-in plugin selection).
- `OPENSNAP_BOOTSTRAP_GAMES`: games whose logins the bootstrap accepts: `generic` (default, every known game) or a comma-separated list of game identifiers. Logins from other games are dropped and logged.
- `OPENSNAP_GAME_SERVER_MAP`: optional explicit `game plugin name -> host:port` bootstrap redirect map. Example: `{"automodellista":"192.168.1.151:9091","monsterhunter":"192.168.1.152:10070"}`. Object values with `host` and `port` are also accepted, but `host:port` is the intended primary form.
- `OPENSNAP_SERVER_SECRET`: bootstrap server secret string.
- `OPENSNAP_BOOTSTRAP_KEY`: bootstrap encryption key string (default: `SNAP-SWAN`).
- `OPENSNAP_TICK_INTERVAL_SECONDS`: periodic tick interval (default: `10.0`).

The bootstrap and game servers are separate processes. Point both at the same shared store (`OPENSNAP_SQLITE_PATH`, or the same MariaDB or PostgreSQL database) so the bootstrap-issued session is available when the client reconnects to the game port.
The bootstrap handshake stays on the bootstrap endpoint through login start and verifier exchange (`0x2c` / `0x41`). The client should not switch to the game endpoint until bootstrap login success returns the final game server IP/port.

Every SN@P title logs in on the same bootstrap port (the SDK hardcodes UDP 9090), so the bootstrap identifies the game from the login itself: each game passes a fixed title code to the SDK, sent at login payload offset 100, and the packet footer tells the SDK generation apart. Known builds: Auto Modellista US `0xCAAD` (beta1 is the `0xCAAD` build with the legacy `0xBA476610` footer), Monster Hunter NA `0xCA03` and EU `0xCA0E` (both served by `monsterhunter`). Logins from unknown builds are dropped and logged, as are games left out of `OPENSNAP_BOOTSTRAP_GAMES`. The redirect target is then resolved through `OPENSNAP_GAME_SERVER_MAP` plus the current process's local game endpoint, so one bootstrap can serve every game, each game server on its own host or port. Game servers may share one SQLite database: rooms and lobby state are kept per game.

## Run Bootstrap Server

Start the standalone bootstrap service:

```bash
python3 run.py bootstrap
```

Expected startup output includes:

```text
Starting openSNAP bootstrap server on 0.0.0.0:9090.
```

## Run Game Server

Start the standalone game service:

```bash
python3 run.py game
```

## Run Web Service

Start the web service (separate process):

```bash
python3 run.py web
```

Run web, bootstrap, and game services against the same shared store so account creation/login from the web page is immediately available to the UDP services.

Expected startup output includes:

```text
openSNAP web listening on 0.0.0.0:80 using plugin <web-game-module>.
```

The web service includes plugin-defined routes based on the original SNAP web flows.

The signup flow is user-driven and shared by every game (`opensnap_web/signup.py`): the page lets the player choose
the username the browser saves to the memory card. Usernames are 4-10 letters, digits and single inner `_` (10 is the
most the client browser keeps, and every game logs in with the whole ID). Passwords are 4-15 characters, the length of
the in-game password keyboard of every client. The form uses simple PS2-era-compatible HTML input controls for old console
browsers. The page acts as create/login: missing users are created in the shared store, existing users must provide
the matching password.

For reverse-engineering support, unknown routes trigger a full terminal request dump (method, URL, query, headers, form/body, and source address).

Web layout: `opensnap_web/app.py` opens the shared store once and registers one Flask blueprint per selected game
module; `opensnap_web/games/__init__.py` lists the modules; `common.py` holds the response, request-dump and route
helpers and `signup.py` the shared signup pages. A game module (`games/monsterhunter/`, `games/automodellista/`)
only declares its paths and pages.

## Run DNS Service

Start the standalone DNS service (separate process):

```bash
python3 run.py dns
```

The DNS service provides static A-record answers from `OPENSNAP_DNS_ENTRIES`.
By default, `OPENSNAP_DNS_ENTRIES` includes entries required by bundled game modules.

The value `@default` in DNS entries resolves to `OPENSNAP_DNS_DEFAULT_IP` when set, otherwise `OPENSNAP_GAME_ADVERTISE_HOST`, then `OPENSNAP_GAME_HOST` (if it is a concrete IPv4), then `127.0.0.1`.

## Storage Configuration

openSNAP keeps two kinds of state:

- Shared store: accounts, the bootstrap-to-game session handoff, and game records for rankings (Auto Modellista
  lap times, Monster Hunter EU quest records). Web, bootstrap, and game services must use the same shared store.
  It is touched at signup, login, and record uploads, never per packet.
- Runtime state: sessions with their packet counters, lobbies, and rooms. Each server process keeps its own in
  memory, so it is gone after a restart (clients log in again anyway).

Shared store settings:

- `OPENSNAP_STORAGE_BACKEND`: `sqlite` (default), `mariadb` or `postgresql`.
- `OPENSNAP_SQLITE_PATH`: SQLite database file (default: `opensnap.db`). Every service must point at the same file, so
  SQLite suits services running on one machine.
- `OPENSNAP_MARIADB_HOST`, `OPENSNAP_MARIADB_PORT` (default `3306`), `OPENSNAP_MARIADB_USER`,
  `OPENSNAP_MARIADB_PASSWORD`, `OPENSNAP_MARIADB_DATABASE` (default `opensnap`), `OPENSNAP_MARIADB_SSL_CA` (optional CA
  file for TLS): MariaDB/MySQL connection for services spread over several machines. Requires `pip install PyMySQL`.
- `OPENSNAP_POSTGRESQL_HOST`, `OPENSNAP_POSTGRESQL_PORT` (default `5432`), `OPENSNAP_POSTGRESQL_USER`,
  `OPENSNAP_POSTGRESQL_PASSWORD`, `OPENSNAP_POSTGRESQL_DATABASE` (default `opensnap`), `OPENSNAP_POSTGRESQL_SSL_CA`
  (optional CA file; connects over TLS and verifies the server name): PostgreSQL connection, the alternative to
  MariaDB for services spread over several machines. Requires `pip install "psycopg[binary]"`. The tables are
  created on first connection.
  openSNAP creates its tables on first start; the user only needs rights on that database. Keep the database
  reachable only by your own servers.
- `OPENSNAP_DEFAULT_USERS`: comma-separated `username:password[:seed[:team]]` entries inserted on startup.

Older SQLite files are migrated automatically: the runtime tables earlier versions kept there are dropped, accounts
are kept.

Example:

```bash
OPENSNAP_SQLITE_PATH=./opensnap.sqlite python3 run.py game
```

## Game Plugin Configuration

Game behavior is loaded through a plugin selected in server config.

Environment variable:

- `OPENSNAP_GAME_PLUGIN`: plugin name to load.

Run with explicit plugin selection:

```bash
OPENSNAP_GAME_PLUGIN=<plugin_name> python3 run.py game
```

## Monster Hunter

Run the game service with the `monsterhunter` plugin:

```bash
OPENSNAP_GAME_PLUGIN=monsterhunter python3 run.py game
```

The bootstrap recognizes Monster Hunter NA and EU logins by their title codes and routes both to the
`monsterhunter` game server (its `OPENSNAP_GAME_SERVER_MAP` entry, or the local game endpoint). Both releases
share the lobby and hunt protocol, so NA and EU players meet in the same Lands, Towns, and quests. EU text is
UTF-8 while NA shows plain ASCII, so accented EU names and chat look garbled on NA screens.

Besides SNAP UDP, the client uses an APP TCP service on port `10127` for its online menu (Land list, Market
state, Event download and quest-return receipts). The `monsterhunter` plugin starts it inside the game process.
Its settings are grouped under "Plugin-specific configuration" in `.env.dist`:

- `OPENSNAP_MH_APP_HOST`: APP bind host (default: `OPENSNAP_GAME_HOST`).
- `OPENSNAP_MH_APP_PORT`: APP TCP port (default: `10127`).
- `OPENSNAP_MH_WORLDS`: nested JSON object of World name ->
  `{"enabled": true, "host": "...", "description": "...", "lands": [...]}`,
  where each Land is `{"key": "...", "name": "...", "description": "...", "areas": N, "capacity": N, "color": "#RRGGBB"}`
  (up to 16 Worlds, 56 Lands per World and 64 in total, 1-26 Areas per Land, Land keys up to 13 characters and
  unique within their World, capacity default 750). The World and Land menus show the highlighted entry's
  `description` (default "Select a world to login to." and "Select a land to login to."). The default is the original Brave World with the Red, Green
  and Blue Lands, in the JP release's Land panel colors (all official colors are listed in `.env.dist`). Exactly
  one World leaves `host` empty: the World served here. A World with a `host` runs on another game server (for
  example another region); the client logs in to that address after choosing it, and this server only lists its
  Lands (populations show as 0). Only enabled Worlds count (`enabled` defaults to `true`); the original Sincere
  World is in `.env.dist` with `"enabled": false`, validated but not listed. Areas are named `<key>01`..`<key>NN`;
  an Area holds at most 63 Towns (3 categories of 21) of `OPENSNAP_MAX_PLAYERS_PER_ROOM` players, and the client
  creates every Town with a maximum of 8.
- `OPENSNAP_DATA_DIR`: local data root (default: `data`). Monster Hunter reads `data/monsterhunter`:
  - `events/manifest.json`: optional downloadable Event quests,
    `{"events": [{"index": 0, "quest_id": 201, "title": "...", "file": "201.mib"}, ...]}`. One quest is
    served per day in `index` order (by day of the year, like the Market), then the list repeats. Files are
    NA/EU quest files of at most 32 KB, and `quest_id` must match the number inside the file.
    `events/manifest.json.dist` is a sample; quest files (`*.mib`) and `manifest.json` are not in the
    repository;
  - `quests.json`: `{"<quest number>": {"name": "...", "category": "..."}}` for the Record pages (all NA/EU
    disc quests and the Event quests, with the EU names). Categories: `hunt`, `gathering`, `capture`,
    `special`, `event`. The Record menu opens an index that links to the monster page (top 3 hunters per
    monster) and to each category's pages (10 quests per page by quest number, top 4 clears each). Quests
    it does not list are shown by number under "Other quests";
  - `information.json`: optional `{"title": "...", "pages": ["page1.txt"]}` online Information pages
    (1-3 pages, 8192 bytes each, client page markup such as `<BODY>`). Without it one empty page is
    published, which the client needs to continue its online setup (Event, Market, connection timing).
    NA only; EU has no such pages.
  - `files/`: pages the client asks for by name: the welcome page shown at the first online connection,
    `files/02/TOP_INFOR.HTM` for NA and `files/03/<language>/TOP_INFOR.HTM` for EU (languages `01` English,
    `02` French, `03` Italian, `04` Spanish, `05` German). A missing file is shown as nothing. The log names
    every requested file.

The data folder is not distributed with openSNAP.

Players stay listed in their Town while they are out on a quest, and return to that Town when the quest ends.

After a quest the EU client uploads its quest records: monsters hunted and the clear time. They are kept in the
shared store (`records`, game `monsterhunter`, boards `hunts-<quest>` and `clear-<quest>`) and shown on the EU lobby's
Record page (Information menu, `DATABASE.HTM`), which the server generates on request: the top hunters by monsters
hunted and the fastest clears of every quest. Quests are listed by number until their names are decoded. A record
from an address with several logged-in players cannot be told apart and is not kept.

The Minegarde Market day changes automatically with the server's local date, following the 10-day
rotation Claw, Normal, Half-off, Normal, Tools, Normal, Half-off, Normal, Fish & Food, Normal.

Account registration uses the `monsterhunter` web module (`/mhweb/...`). DNAS is not served by openSNAP: point
the DNAS host at an external DNAS-compatible service through `OPENSNAP_DNS_ENTRIES`.

## Web Service Configuration

Environment variables for the Flask service:

- `OPENSNAP_WEB_HOST`: bind host (default: `0.0.0.0`).
- `OPENSNAP_WEB_PORT`: bind port (default: `80`).
- `OPENSNAP_WEB_HTTPS_HOST`: HTTPS bind host for `rankweb` (default: value of `OPENSNAP_WEB_HOST`).
- `OPENSNAP_WEB_HTTPS_PORT`: HTTPS bind port for `rankweb` (default: `443`).
- `OPENSNAP_WEB_HTTPS_CERTFILE`: certificate path for the optional HTTPS listener.
- `OPENSNAP_WEB_HTTPS_KEYFILE`: private key path for the optional HTTPS listener.
- `OPENSNAP_WEB_GAME_PLUGIN`: web route mode/profile (default: `generic`).
  - `generic`: one web server serves routes from all bundled web modules.
  - explicit module name: register only that module (`automodellista`, `automodellista_beta1`, `monsterhunter`).

Example:

```bash
OPENSNAP_WEB_PORT=80 python3 run.py web
```

Override to a single explicit profile if needed:

```bash
python3 run.py web --web-plugin automodellista_beta1
```

Games leave the SNAP UDP flow after post-game lobby leave and enters
its web/database flow. The embedded info pages use `http://gameweb...`, while
the ranking upload path uses `https://rankweb...`. Run the web service for the
post-game return path, and configure the optional HTTPS listener if you want to
serve the embedded `rankweb` URL locally.

Auto Modellista rankings: each upload's best lap is kept per course for an existing account, and the ranking
page lists each player's best lap, fastest first: 10 per course, plus 50 on the release event board.

## DNS Service Configuration

Environment variables for the DNS service:

- `OPENSNAP_DNS_HOST`: bind host (default: `0.0.0.0`).
- `OPENSNAP_DNS_PORT`: bind port (default: `53`).
- `OPENSNAP_DNS_TTL`: TTL for answered A records (default: `60`).
- `OPENSNAP_DNS_DEFAULT_IP`: optional fallback IPv4 for default module records.
- `OPENSNAP_DNS_ENTRIES`: static DNS entries as a dict (JSON object or Python dict literal), where keys are hostnames and values are IPv4 strings or `@default`.
  Wildcards are supported in keys (for example `*.games.sega.net`).
  The `.env` parser supports multi-line dict blocks so you can group entries per game and add comments.

Example:

```dotenv
OPENSNAP_DNS_ENTRIES={
  # Game A
  "bootstrap.game-a.example.net": "@default",
  "gameweb.game-a.example.net": "@default",
  "regweb.game-a.example.net": "@default"
}
```

Binding to port `53` needs elevated permissions on Linux (for example with `sudo`).
This restriction does not apply on Windows.
macOS behavior is environment-dependent.

For domains not defined by static entries, openSNAP falls back to the host system DNS resolver.

## Logging Configuration

`openSNAP` uses Python's standard logging module with runtime level selection.

Environment variables:

- `OPENSNAP_LOG_LEVEL`: one of `debug`, `info`, `warn`, `warning`, `error`, `critical` (default: `debug`).
- `OPENSNAP_LOG_PATH`: optional log directory path. When set, logs are written to both stdout and a per-service file in that directory.
  Bootstrap uses `opensnap-bootstrap.log`, game uses `opensnap-game.log`, DNS uses `opensnap-dns-log`, and web uses `opensnap-web.log`.
- `OPENSNAP_LOG_HEXDUMP_LIMIT`: max bytes rendered in packet hexdumps (default: `16384`). Set to `0` for unlimited output.

Examples:

```bash
OPENSNAP_LOG_LEVEL=debug python3 run.py game
```

```bash
OPENSNAP_LOG_LEVEL=debug OPENSNAP_LOG_HEXDUMP_LIMIT=4096 python3 run.py game
```

```bash
OPENSNAP_LOG_LEVEL=debug OPENSNAP_LOG_PATH=./logs python3 run.py game
```

With `debug` level enabled, received UDP datagrams include a formatted hexdump in logs.

## WSGI Deployment

The web service is WSGI-compatible and exposes these callables:

- `opensnap_web.wsgi:app`
- `opensnap_web.wsgi:application`

Example with Gunicorn:

```bash
gunicorn opensnap_web.wsgi:app --bind 0.0.0.0:80
```

Example with Nginx Unit:

- module: `opensnap_web.wsgi`
- callable: `app` (or `application`)

## Run Tests

Run the full suite:

```bash
python3 -m unittest discover -s tests -v
```

Note: replay regression tests use optional local packet-capture logs. If those logs are not present, replay tests are skipped automatically.

## Project Layout

- `opensnap/protocol`: wire models, constants, and packet codec.
- `opensnap/core`: engine, auth, routing, and shared state services.
- `opensnap/storage`: backend factory and storage implementations.
- `opensnap/plugins`: extension points for game-specific behavior.
- `opensnap_web`: separate web bootstrap/login service package.
- `opensnap_dns`: separate standalone DNS service package.
- `tests`: unit and regression tests.

## Acknowledgements

- This project has been possible thanks to No23 and his previous private work on `snapsi`.
- flyinghead's `kage_server` repository was used as a reference while understanding parts of the protocol behavior.
- LLMs were used to help reverse engineer binary ELF game files with the aid of `objdump`, `radare2`, `mips-linux-gnu-objdump`, and `readelf` CLI tools.
