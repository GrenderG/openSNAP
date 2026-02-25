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

5. Verify imports.

```bash
python3 -c "import cryptography; import opensnap"
```

## Run openSNAP (UDP)

Start the SNAP UDP service:

```bash
python3 run.py udp
```

Expected startup output includes:

```text
openSNAP listening on 0.0.0.0:9090 using plugin automodellista.
```

Stop the server with `Ctrl+C`.

`python3 run.py` without arguments is equivalent to `python3 run.py udp`.

## Core Server Configuration

Environment variables for the UDP server:

- `OPENSNAP_HOST`: bind host (default: `0.0.0.0`).
- `OPENSNAP_PORT`: bind port (default: `9090`).
- `OPENSNAP_GAME_PLUGIN`: game plugin name (default: `automodellista`).
- `OPENSNAP_SERVER_SECRET`: bootstrap server secret string.
- `OPENSNAP_BOOTSTRAP_KEY`: bootstrap encryption key string (default: `SNAP-SWAN`).
- `OPENSNAP_TICK_INTERVAL_SECONDS`: periodic tick interval (default: `10.0`).

## Run Web Bootstrap Service

Start the web service (separate process):

```bash
python3 run.py web
```

Run web and UDP services with the same `OPENSNAP_SQLITE_PATH` so account creation/login from the web page is immediately available to the UDP server.

Expected startup output includes:

```text
openSNAP web listening on 0.0.0.0:80 using plugin automodellista.
```

The web service includes routes based on `snapsi/www`:

- `/login.php`
- `/amweb/index.jsp`
- `/amweb/create_id.html` (`username` and `password` parameters)
- `/amweb/create_id_<username>.html` (password can be provided as query/body parameter)
- `/amusa/am_info.html`
- `/amusa/am_rule.html`
- `/amusa/am_rank.html`
- `/amusa/am_taboo.html`
- `/amusa/am_up.php`

The signup flow is user-driven. `/amweb/index.jsp` provides a form where the player chooses the username to encode in the signup payload.
Usernames are limited to 10 characters and accepted characters are `A-Z`, `a-z`, `0-9`, `_`, `.`, and `-`.
Passwords are required and limited to 8 characters.
The form uses simple legacy-compatible HTML input controls for old console browsers.
The page acts as create/login: missing users are created in SQLite, existing users must provide the matching password.

For reverse-engineering support, unknown routes trigger a full terminal request dump (method, URL, query, headers, form/body, and source address).

Web routes are modular by game plugin. Each game can implement its own route set in `opensnap_web/games`.

## Storage Configuration

openSNAP uses SQLite storage by default.

Environment variables:

- `OPENSNAP_SQLITE_PATH`: path to SQLite database file (default: `opensnap.db`).
- `OPENSNAP_RESET_RUNTIME_ON_STARTUP`: clears runtime tables (`sessions`, `rooms`, `room_members`) when the server starts (default: `true`).
- `OPENSNAP_DEFAULT_USERS`: users seeded at startup as `username:password[:seed[:team]]`, comma-separated.

The default `.env.dist` includes `test:1111` in `OPENSNAP_DEFAULT_USERS`.

Run with custom SQLite path:

```bash
OPENSNAP_SQLITE_PATH=./opensnap.sqlite python3 run.py udp
```

## Game Plugin Configuration

Game behavior is loaded through a plugin selected in server config.

Environment variable:

- `OPENSNAP_GAME_PLUGIN`: plugin name to load (default: `automodellista`).

Run with explicit plugin selection:

```bash
OPENSNAP_GAME_PLUGIN=automodellista python3 run.py udp
```

## Web Service Configuration

Environment variables for the Flask service:

- `OPENSNAP_WEB_HOST`: bind host (default: `0.0.0.0`).
- `OPENSNAP_WEB_PORT`: bind port (default: `80`).
- `OPENSNAP_WEB_GAME_PLUGIN`: web game module name (default: value of `OPENSNAP_GAME_PLUGIN`, otherwise `automodellista`).

Example:

```bash
OPENSNAP_WEB_PORT=80 python3 run.py web
```

## Logging Configuration

`openSNAP` uses Python's standard logging module with runtime level selection.

Environment variables:

- `OPENSNAP_LOG_LEVEL`: one of `debug`, `info`, `warn`, `warning`, `error`, `critical` (default: `debug`).
- `OPENSNAP_LOG_HEXDUMP_LIMIT`: max bytes rendered in packet hexdumps (default: `16384`). Set to `0` for unlimited output.

Examples:

```bash
OPENSNAP_LOG_LEVEL=debug python3 run.py udp
```

```bash
OPENSNAP_LOG_LEVEL=debug OPENSNAP_LOG_HEXDUMP_LIMIT=4096 python3 run.py udp
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
- `tests`: unit and regression tests.

## Acknowledgements

This project has been possible thanks to No23 and his previous private work on `snapsi`.

## Troubleshooting

- `python3: command not found`: install Python 3.11+ and reopen your shell.
- `No module named 'opensnap'`: start services from the repository root using `python3 run.py udp` or `python3 run.py web`.
- `Address already in use`: another process is using UDP port `9090`; stop that process or change server settings before starting openSNAP.
