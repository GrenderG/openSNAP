# openSNAP

Open, clean-room implementation of the Sega Network Application Package (SN@P).

## Overview

openSNAP is designed with a strict separation between:

- protocol and transport core logic,
- state and storage logic,
- game-specific extensions.

This keeps core behavior reusable while allowing game integrations to be added independently.

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

3. Upgrade packaging tools and install openSNAP.

```bash
python -m pip install --upgrade pip setuptools wheel
pip install -e .
```

4. Verify installation.

```bash
python -c "import opensnap; print(opensnap.__version__)"
```

## Run openSNAP

Start the server:

```bash
opensnap-server
```

Expected startup output includes:

```text
openSNAP listening on 0.0.0.0:9090.
```

Stop the server with `Ctrl+C`.

## Storage Configuration

By default, openSNAP uses in-memory storage.

Environment variables:

- `OPENSNAP_STORAGE_BACKEND`: `memory` (default) or `sqlite`.
- `OPENSNAP_SQLITE_PATH`: path to SQLite file when backend is `sqlite` (default: `opensnap.db`).

Run with SQLite:

```bash
OPENSNAP_STORAGE_BACKEND=sqlite OPENSNAP_SQLITE_PATH=./opensnap.sqlite opensnap-server
```

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
- `tests`: unit and regression tests.

## Acknowledgements

This project has been possible thanks to No23 and his previous private work on `snapsi`.

## Troubleshooting

- `python3: command not found`: install Python 3.11+ and reopen your shell.
- `opensnap-server: command not found`: make sure your virtual environment is activated and `pip install -e .` completed successfully.
- `Address already in use`: another process is using UDP port `9090`; stop that process or change server settings before starting openSNAP.
