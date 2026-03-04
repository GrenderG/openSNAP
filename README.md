# openSNAP

Open, clean-room implementation of the Sega Network Application Package (SN@P).

## Overview

openSNAP is designed with a strict separation between:

- protocol and transport core logic,
- state and storage logic,
- game-specific extensions.

This keeps core behavior reusable while allowing game integrations to be added independently.

## Project Status

openSNAP is still in an early work-in-progress stage.

More help is needed, especially for protocol research, packet analysis, compatibility testing, and implementation work.

## SNAP History (Brief)

SN@P started as **SEGA Network Application Package**, created by Sega.com as middleware for online multiplayer game services.

In the early 2000s, SEGA positioned SNAP as a cross-platform networking stack for game developers. In **December 2002**, Sega.com announced middleware agreements that made SNAP available to PlayStation 2 and GameCube developers.

On **August 19, 2003**, Nokia and SEGA announced an agreement for Nokia to acquire select Sega.com technology, including SNAP. Nokia stated that SNAP would become core technology for its online mobile gaming push (especially around N-Gage services).

After that transition, the platform was commonly referred to in Nokia's mobile ecosystem as **SNAP Mobile**, and industry coverage from that period described SNAP as **Scalable Network Application Package** in its Nokia-era mobile form.

Historical references:

- Nokia/SEGA transfer announcement (Aug 19, 2003): https://www.globenewswire.com/news-release/2003/08/19/1847054/0/en/Nokia-and-SEGA-reach-agreement-on-the-transfer-of-select-SEGA-com-leading-technology.html
- Sega middleware rollout coverage (Dec 4, 2002): https://www.gamedeveloper.com/game-platforms/sega-networking-middleware-rolls-out-to-ps2-gamecube-developers
- Nokia/Sun SNAP Mobile coverage (Jul 1, 2004): https://www.gamespot.com/articles/nokia-and-sun-bringing-snap-to-java-handsets/1100-6101766/

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
- `OPENSNAP_GAME_IDENTIFIER`: identifier for the game served by this game process (default: `automodellista`).
- `OPENSNAP_GAME_PLUGIN`: game plugin name (default: built-in plugin selection).
- `OPENSNAP_BOOTSTRAP_DEFAULT_GAME_IDENTIFIER`: bootstrap fallback game id used when no reliable per-game identifier is available from the UDP login flow.
- `OPENSNAP_GAME_SERVER_MAP`: optional explicit `game_identifier -> host:port` bootstrap redirect map. Example: `{"automodellista":"192.168.1.151:9091","monsterhunter":"192.168.1.152:10070"}`. Object values with `host` and `port` are also accepted, but `host:port` is the intended primary form.
- `OPENSNAP_SERVER_SECRET`: bootstrap server secret string.
- `OPENSNAP_BOOTSTRAP_KEY`: bootstrap encryption key string (default: `SNAP-SWAN`).
- `OPENSNAP_TICK_INTERVAL_SECONDS`: periodic tick interval (default: `10.0`).

The bootstrap and game servers are separate processes. Keep both pointed at the same `OPENSNAP_SQLITE_PATH` so the bootstrap-issued session id is available when the client reconnects to the game port.
The bootstrap handshake stays on the bootstrap endpoint through login start and verifier exchange (`0x2c` / `0x41`). The client should not switch to the game endpoint until bootstrap login success returns the final game server IP/port.

The bootstrap UDP layer does not receive the original web-style bootstrap URL through the current transport API, so the server cannot reliably infer the requested game from hostname or URL today. Bootstrap routing therefore uses the configured `OPENSNAP_BOOTSTRAP_DEFAULT_GAME_IDENTIFIER` unless a future protocol-level identifier is confirmed. The redirect target is then resolved through the explicit `OPENSNAP_GAME_SERVER_MAP` plus the current process's local game endpoint.

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

Run web, bootstrap, and game services with the same `OPENSNAP_SQLITE_PATH` so account creation/login from the web page is immediately available to the UDP services.

Expected startup output includes:

```text
openSNAP web listening on 0.0.0.0:80 using plugin <web-game-module>.
```

The web service includes plugin-defined routes based on the original SNAP web flows.

The signup flow is user-driven. The plugin-provided signup page allows the player to choose the username encoded in the signup payload.
Usernames are limited to 10 characters and accepted characters are `A-Z`, `a-z`, `0-9`, `_`, `.`, and `-`.
Passwords are required and limited to 8 characters.
The form uses simple PS2-era-compatible HTML input controls for old console browsers.
The page acts as create/login: missing users are created in SQLite, existing users must provide the matching password.

For reverse-engineering support, unknown routes trigger a full terminal request dump (method, URL, query, headers, form/body, and source address).

Web routes are modular by game plugin. Each game can implement its own route set in `opensnap_web/games`.

## Run DNS Service

Start the standalone DNS service (separate process):

```bash
python3 run.py dns
```

The DNS service provides static A-record answers from `OPENSNAP_DNS_ENTRIES`.
By default, `OPENSNAP_DNS_ENTRIES` includes entries required by bundled game modules.

The value `@default` in DNS entries resolves to `OPENSNAP_DNS_DEFAULT_IP` when set, otherwise `OPENSNAP_GAME_ADVERTISE_HOST`, then `OPENSNAP_GAME_HOST` (if it is a concrete IPv4), then `127.0.0.1`.

## Storage Configuration

openSNAP uses SQLite storage by default.

Environment variables:

- `OPENSNAP_SQLITE_PATH`: path to SQLite database file (default: `opensnap.db`).
- `OPENSNAP_RESET_RUNTIME_ON_STARTUP`: clears transient runtime state on startup (default: `true`). In split mode, the bootstrap server preserves sessions and the game server clears room state without deleting bootstrap-issued sessions.
- `OPENSNAP_DEFAULT_USERS`: users seeded at startup as `username:password[:seed[:team]]`, comma-separated.

The default `.env.dist` includes `test:1111` in `OPENSNAP_DEFAULT_USERS`.

Run with custom SQLite path:

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

## Web Service Configuration

Environment variables for the Flask service:

- `OPENSNAP_WEB_HOST`: bind host (default: `0.0.0.0`).
- `OPENSNAP_WEB_PORT`: bind port (default: `80`).
- `OPENSNAP_WEB_HTTPS_HOST`: HTTPS bind host for `rankweb` (default: value of `OPENSNAP_WEB_HOST`).
- `OPENSNAP_WEB_HTTPS_PORT`: HTTPS bind port for `rankweb` (default: `443`).
- `OPENSNAP_WEB_HTTPS_CERTFILE`: certificate path for the optional HTTPS listener.
- `OPENSNAP_WEB_HTTPS_KEYFILE`: private key path for the optional HTTPS listener.
- `OPENSNAP_WEB_GAME_PLUGIN`: web game module name (default: value of `OPENSNAP_GAME_PLUGIN`; bundled modules include `automodellista` and `monsterhunter`).

Example:

```bash
OPENSNAP_WEB_PORT=80 python3 run.py web
```

Auto Modellista leaves the SNAP UDP flow after post-game lobby leave and enters
its web/database flow. The embedded info pages use `http://gameweb...`, while
the ranking upload path uses `https://rankweb...`. Run the web service for the
post-game return path, and configure the optional HTTPS listener if you want to
serve the embedded `rankweb` URL locally.

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

This project has been possible thanks to No23 and his previous private work on `snapsi`.

## Troubleshooting

- `python3: command not found`: install Python 3.11+ and reopen your shell.
- `No module named 'opensnap'`: start services from the repository root using `python3 run.py game`, `python3 run.py bootstrap`, or `python3 run.py web`.
- `Address already in use`: another process is using the configured bootstrap or game UDP port; stop that process or change server settings before starting openSNAP.
