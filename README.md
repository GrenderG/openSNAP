# openSNAP

An open, clean-room server for SEGA's SN@P online middleware, bringing PlayStation 2 online games back.

## Supported games

| Game | Serial | Plugin | Status |
| --- | --- | --- | --- |
| Auto Modellista Public Beta volume 1.0 | `SLUS_204.98` | `automodellista_beta1` | Supported. |
| Auto Modellista Public Beta volume 2.0 | `SLUS_280.31` | `automodellista` | Supported (same protocol as the release). |
| Auto Modellista NTSC-U/C | `SLUS_206.42` | `automodellista` | Supported. |
| Monster Hunter NTSC-U | `SLUS_208.96` | `monsterhunter` | Supported. Plays with PAL. |
| Monster Hunter PAL | `SLES_527.07` | `monsterhunter` | Supported. Plays with NTSC-U; accented EU text looks garbled on NTSC-U screens. |
| Monster Hunter PAL demo | `SLED_530.83` | `monsterhunter` | Expected to work with the releases; untested. DNAS patch required. |
| Monster Hunter PAL test build | `TLES_527.07` | `monsterhunter` | Supported, untested. Plays with the releases. DNAS patch required. |
| Monster Hunter NTSC-U public beta | `SLUS_291.10` | `monsterhunter_na_beta` | Supported, untested. Separate server: beta players only meet each other. DNAS patch required. |
| Resident Evil Outbreak (File#1) NTSC-U | `SLUS_207.65` | `outbreak` | Supported, v1 and v2 play together. Not yet fully tested in game. |
| Resident Evil Outbreak (File#1) NTSC-U public beta | `SLUS_290.89` | - | Not supported: same login as the release, but an earlier lobby design (own Areas `obbs01`/`obbf01`, fixed "TEST GAME" rooms, older APP flow) that would need its own reverse engineering. |

Monster Hunter and Resident Evil Outbreak also need the [Capcom APP service](#capcom-app-service).

openSNAP does not serve DNAS: point the DNAS hostnames (listed in `.env.dist`) at a DNAS replay server such as
dnasrep-go. The builds marked "DNAS patch required" need a DNAS-patched disc instead, because their DNAS replies
were never captured.

## Install

Requirements: `git` and Python 3.11 or newer.

```bash
git clone https://github.com/GrenderG/openSNAP
cd openSNAP
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.dist .env                  # optional: created automatically on first run
```

All settings live in `.env`, documented in `.env.dist`. `.env` is git-ignored.

## Services

openSNAP runs as separate processes. Run the ones your games need, all using the same [shared store](#storage):

| Command | Service | Default port |
| --- | --- | --- |
| `python3 run.py bootstrap` | Bootstrap login, used by every game | UDP 9090 (fixed by the games) |
| `python3 run.py game` | Game server for one plugin (`python3 run.py` alone does the same) | UDP 9091 |
| `python3 run.py web` | Signup pages and rankings | TCP 80 (optional HTTPS 443) |
| `python3 run.py dns` | DNS that points the games' hostnames at your servers | UDP 53 |
| `python3 run.py app capcom` | Capcom APP service (Monster Hunter, Resident Evil Outbreak) | TCP 10127 (fixed by the games) |

Open or forward these ports if players connect from outside your network.

### Bootstrap and game servers

Every game logs in on the bootstrap first. The bootstrap identifies the game from the login (its SDK title code
and packet footer) and sends the client on to that game's server. One bootstrap serves every game; each game runs
its own game server.

- `OPENSNAP_GAME_PLUGIN`: plugin the game server loads (see the table above).
- `OPENSNAP_GAME_SERVER_MAP`: where the bootstrap sends each game, e.g.
  `{"automodellista":"192.168.1.151:9091","monsterhunter":"192.168.1.152:10070"}`. A game missing from the map goes
  to this machine's game server.
- `OPENSNAP_BOOTSTRAP_GAMES`: games this bootstrap accepts: `generic` (default, all) or a comma-separated list. It
  also separates builds that share a title code (see [Monster Hunter NA public beta](#monster-hunter-na-public-beta)).
- `OPENSNAP_BOOTSTRAP_HOST`, `OPENSNAP_BOOTSTRAP_PORT`: bootstrap bind address (default `0.0.0.0:9090`).
- `OPENSNAP_GAME_HOST`, `OPENSNAP_GAME_PORT`: game server bind address (default `0.0.0.0:9091`). Any game port
  works: the bootstrap tells it to the client.
- `OPENSNAP_BOOTSTRAP_ADVERTISE_HOST`, `OPENSNAP_GAME_ADVERTISE_HOST`: IPv4 given to clients when the bind address
  isn't reachable as is (empty: derived from the bind host and routing).
- `OPENSNAP_SERVER_SECRET`, `OPENSNAP_BOOTSTRAP_KEY` (default `SNAP-SWAN`): bootstrap login secrets.
- `OPENSNAP_MAX_ROOMS_PER_LOBBY`, `OPENSNAP_MAX_PLAYERS_PER_ROOM`: room limits.
- `OPENSNAP_TICK_INTERVAL_SECONDS`: housekeeping interval (default `10.0`).

### Web service

Serves account signup for every game, plus game pages and rankings.

- `OPENSNAP_WEB_HOST`, `OPENSNAP_WEB_PORT`: bind address (default `0.0.0.0:80`).
- `OPENSNAP_WEB_HTTPS_HOST`, `OPENSNAP_WEB_HTTPS_PORT`, `OPENSNAP_WEB_HTTPS_CERTFILE`, `OPENSNAP_WEB_HTTPS_KEYFILE`:
  optional HTTPS listener (port 443), used by Auto Modellista's `rankweb` uploads.
- `OPENSNAP_WEB_GAME_PLUGIN`: `generic` (default, every game's pages) or one module: `automodellista`,
  `automodellista_beta1`, `monsterhunter`, `outbreak`. Also `python3 run.py web --web-plugin <module>`.

Signup creates the account on first use and logs in afterwards. Usernames are 4-10 letters, digits and single inner
`_` (the longest ID the game browsers keep). Passwords are 4-15 characters (the in-game keyboard limit).

Requests to unknown paths are logged in full, to help with reverse engineering.

For a WSGI server, use `opensnap_web.wsgi:app` (or `application`). It reads `.env` and logs like `run.py web`; run
it from the repository directory so relative paths in `.env` resolve.

```bash
gunicorn opensnap_web.wsgi:app --bind 0.0.0.0:80
```

```apache
# Apache mod_wsgi: one process, since the app opens one shared-store connection.
WSGIDaemonProcess opensnap processes=1 threads=8 home=/path/to/openSNAP python-home=/path/to/openSNAP/.venv python-path=/path/to/openSNAP
WSGIProcessGroup opensnap
WSGIApplicationGroup %{GLOBAL}
WSGIScriptAlias / /path/to/openSNAP/opensnap_web/wsgi.py
```

### DNS service

Answers the games' hostnames with your servers' addresses; other names go to the system resolver.

- `OPENSNAP_DNS_ENTRIES`: hostname -> IPv4 map. `.env.dist` lists every hostname the supported games use, grouped
  per game. Wildcards work (`*.games.sega.net`). `@default` means `OPENSNAP_DNS_DEFAULT_IP`, else
  `OPENSNAP_GAME_ADVERTISE_HOST`, else `OPENSNAP_GAME_HOST` (if an IPv4), else `127.0.0.1`.
- `OPENSNAP_DNS_HOST`, `OPENSNAP_DNS_PORT` (default `0.0.0.0:53`), `OPENSNAP_DNS_TTL` (default `60`).

Port 53 needs root on Linux.

### Capcom APP service

Capcom's games also talk to Capcom's own APP service over TCP, beside SN@P. They all connect to
`app01.reo.capcom.sf.yav4.com:10127`, so one process serves every Capcom game and tells them apart from each
connection's first answer.

- Monster Hunter: World and Land menus, Market, Events, quest records.
- Resident Evil Outbreak: lobby welcome page, data files, scenario results and the DATABASE rankings.

It needs the same storage and game settings as the game servers (`OPENSNAP_MH_WORLDS`, `OPENSNAP_DATA_DIR`), and
finds the Monster Hunter game server through `OPENSNAP_GAME_SERVER_MAP` like the bootstrap does.

- `OPENSNAP_CAPCOM_APP_HOST`, `OPENSNAP_CAPCOM_APP_PORT`: bind address (default `0.0.0.0:10127`).

## Storage

The shared store holds accounts, the bootstrap-to-game login handoff and the records behind the rankings. Every
service must use the same one. Lobbies, rooms and sessions live in each process's memory and are lost on restart
(players simply log in again).

- `OPENSNAP_STORAGE_BACKEND`: `sqlite` (default), `mariadb` or `postgresql`.
- `OPENSNAP_SQLITE_PATH`: SQLite file (default `opensnap.db`), for services on one machine.
- `OPENSNAP_MARIADB_HOST`, `_PORT` (3306), `_USER`, `_PASSWORD`, `_DATABASE` (`opensnap`), `_SSL_CA` (optional):
  MariaDB/MySQL, for services on several machines. Needs `pip install PyMySQL`.
- `OPENSNAP_POSTGRESQL_HOST`, `_PORT` (5432), `_USER`, `_PASSWORD`, `_DATABASE` (`opensnap`), `_SSL_CA` (optional,
  enables verified TLS): PostgreSQL, the alternative to MariaDB. Needs `pip install "psycopg[binary]"`.
- `OPENSNAP_DEFAULT_USERS`: `username:password[:seed[:team]]` accounts created at startup (comma-separated).

Tables are created on first start. Keep the database reachable only by your servers.

## Games

### Auto Modellista

```bash
OPENSNAP_GAME_PLUGIN=automodellista python3 run.py game        # release and public beta 2
OPENSNAP_GAME_PLUGIN=automodellista_beta1 python3 run.py game  # public beta 1
```

After a race the game leaves SN@P for web pages: `http://gameweb...` for information and `https://rankweb...` for
the lap-time upload, so run the web service, with HTTPS for uploads. Each account's best lap per course is kept;
the rankings show the 10 fastest per course and 50 on the release's event board.

### Monster Hunter

```bash
OPENSNAP_GAME_PLUGIN=monsterhunter python3 run.py game
```

NA and EU players meet in the same Lands, Towns and quests; the PAL demo and test build join them. Accounts are
created at the `monsterhunter` web pages (`/mhweb/...`).

Settings:

- `OPENSNAP_MH_WORLDS`: the Worlds and Lands of the online menu, as JSON:
  World name -> `{"enabled": true, "host": "...", "description": "...", "lands": [...]}`, each Land
  `{"key": "...", "name": "...", "description": "...", "areas": N, "capacity": N, "color": "#RRGGBB"}`.
  - Default: the original Brave World with the Red, Green and Blue Lands. `.env.dist` also lists the official Land
    colors and the original Sincere World (disabled).
  - Exactly one enabled World has an empty `host`: the one this server hosts. A World with a `host` is another game
    server, which this server only lists.
  - Limits: 16 Worlds, 56 Lands per World and 64 in total, 1-26 Areas per Land, Land keys of up to 13 characters
    (unique per World), capacity 750 by default. Areas are named `<key>01`..`<key>NN` and hold up to 63 Towns.
- `OPENSNAP_DATA_DIR` (default `data`): Monster Hunter reads `data/monsterhunter/`:
  - `files/`: welcome pages, `02/TOP_INFOR.HTM` (NA) and `03/<language>/TOP_INFOR.HTM` (EU: `01` English, `02`
    French, `03` Italian, `04` Spanish, `05` German). Samples are included.
  - `events/manifest.json`: downloadable Event quests, one per day in `index` order:
    `{"events": [{"index": 0, "quest_id": 201, "title": "...", "file": "201.mib"}]}`. Quest files (NA/EU, up to
    32 KB, `quest_id` matching the file) are not included; see `manifest.json.dist`.
  - `quests.json`: quest names and categories (`hunt`, `gathering`, `capture`, `special`, `event`) for the Record
    pages. Included.
  - `information.json`: optional NA Information pages, `{"title": "...", "pages": ["page1.txt"]}` (1-3 pages, 8 KB
    each, game page markup).

Behaviour:

- The Minegarde Market follows the original 10-day rotation, by the server's date.
- Players stay listed in their Town during a quest and return to it afterwards.
- Quest records (monsters hunted, clear times) are kept and shown on the in-game Record pages: top hunters per
  monster and fastest clears per quest. The PAL test build's monster kill totals get a page of their own. A record
  from an address with several players logged in can't be attributed, so it is skipped.

#### Monster Hunter NA public beta

```bash
OPENSNAP_GAME_PLUGIN=monsterhunter_na_beta python3 run.py game
```

The beta's player profile is larger than the release's (216 bytes against 140), so it can't share a server with
it. It also logs in with the release's title code, so it needs its own bootstrap:

- The beta connects to `snap01.reo.capcom.sf.yav4.com` (shared with Resident Evil Outbreak); the release uses
  `bootstrap01.mh-beta.capcom.sf.yav4.com`.
- The beta's bootstrap sets `OPENSNAP_BOOTSTRAP_GAMES=monsterhunter_na_beta,outbreak`. If the release is served too,
  its bootstrap needs another IP (the bootstrap port is fixed) with `OPENSNAP_BOOTSTRAP_GAMES=monsterhunter`, and
  the DNS map points each hostname at its bootstrap.

The Capcom APP service recognises the beta and gives it its own Worlds (`OPENSNAP_MH_NA_BETA_WORLDS`, same format,
default Brave World) on the `monsterhunter_na_beta` entry of `OPENSNAP_GAME_SERVER_MAP`, and its own data folder
(`data/monsterhunter_na_beta`). Beta players sign up on the Outbreak Notice Board page.

### Resident Evil Outbreak

```bash
OPENSNAP_GAME_PLUGIN=outbreak python3 run.py game
```

Both releases (v1 and v2) play together.

- Lobby: the free-mode hall, five scenario-mode Areas and `OPENSNAP_OUTBREAK_FREE_AREAS` free-mode Areas (1-99,
  default 10), each holding `OPENSNAP_OUTBREAK_AREA_CAPACITY` players (default 100). Rooms hold up to 4.
- Room rules: the host can change the room title, password, scenario, number of players, waiting time, difficulty
  and friendly fire (off by default).
- Kept from the original game:
  - Scenarios unlock by clearing them, so a fresh save only offers "Outbreak".
  - A free-mode room disbands when its waiting time ends with fewer than 2 players. Scenario mode starts anyway,
    so solo play works there.
- Rankings: every scenario result is stored. The fastest clears per scenario (scenario and free mode) and total
  result points appear in the lobby's DATABASE menu and on the Notice Board.
- Web: the registration menu and the Notice Board open `regweb.reo.capcom.sf.yav4.com/reweb/`, served by the
  `outbreak` web module: account signup plus the rankings.
- Capcom APP: on each lobby entry the game downloads a welcome page and two data files (the scenario list and the
  room rules, generated by `opensnap_app/capcom/outbreak/netbio.py`); after each scenario it uploads its result.
  The game shows `ERR:D9xx` when that upload fails.
- `OPENSNAP_DATA_DIR`: the welcome page is `data/outbreak/files/01/TOP_INFOR.HTM` (included; at most 4095 bytes,
  game page markup such as `<BODY><SIZE=2><CENTER>...<END>`).

## Logging

- `OPENSNAP_LOG_LEVEL`: `debug` (default), `info`, `warning`, `error` or `critical`. `debug` adds packet hexdumps.
- `OPENSNAP_LOG_PATH`: also write logs to this directory, one file per service (`opensnap-game.log`, ...).
- `OPENSNAP_LOG_HEXDUMP_LIMIT`: bytes per hexdump (default `16384`, `0` for no limit).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Replay tests use local packet captures and are skipped when those are missing.

## Project layout

- `opensnap/protocol`: packet format and codec.
- `opensnap/core`: engine, login, routing and shared state.
- `opensnap/storage`: shared-store backends.
- `opensnap/plugins`: one package per game, plus `common` helpers.
- `opensnap_web`: web service; one module per game in `games/`, shared signup in `signup.py`.
- `opensnap_dns`: DNS service.
- `opensnap_app`: companion services beside SN@P (`run.py app <name>`); `capcom` holds the Capcom APP protocol, one
  package per game.
- `data`: game data served at runtime.
- `tests`: unit and regression tests.

## History

SN@P (SEGA Network Application Package) grew out of KAGE, SEGA's online middleware for Dreamcast games. In
December 2002 SEGA opened it to PlayStation 2 and GameCube developers. In August 2003 Nokia acquired it, and it
became SNAP Mobile for the N-Gage.

- [Nokia/SEGA transfer announcement (Aug 19, 2003)](https://www.globenewswire.com/news-release/2003/08/19/1847054/0/en/Nokia-and-SEGA-reach-agreement-on-the-transfer-of-select-SEGA-com-leading-technology.html)
- [SEGA middleware rollout (Dec 4, 2002)](https://www.gamedeveloper.com/game-platforms/sega-networking-middleware-rolls-out-to-ps2-gamecube-developers)
- [Nokia and Sun bring SNAP to Java handsets (Jul 1, 2004)](https://www.gamespot.com/articles/nokia-and-sun-bringing-snap-to-java-handsets/1100-6101766/)

## Acknowledgements

- No23, for his earlier private work on `snapsi`.
- flyinghead's `kage_server`, used as a protocol reference.
- LLMs helped reverse engineer the game binaries with `objdump`, `radare2`, `mips-linux-gnu-objdump` and `readelf`.
