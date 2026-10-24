"""Monster Hunter code patch blobs and the `data/monsterhunter/patches` folder.

The blob format, keys and resident addresses are documented in
`opensnap_app/capcom/monsterhunter/patch.py`; the server offers what the
builders write here when its entry is enabled. A blob is only the plain
`M-HUNTER` patch: retail NA/EU install it only behind a Sony-signed 0x580 DNAS
install header, which cannot be made here (see `patch.py`), so these blobs
cannot be distributed as they are and their entries are written disabled.
"""

from collections.abc import Callable, Sequence
import json
from pathlib import Path
import struct

_BLOB_HEADER = struct.Struct('<8sI')
_ENTRY_HEADER = struct.Struct('<3I')
# A key no module has: the entry is never copied and its data stays resident.
RESIDENT_DATA_KEY = 0xFFFFFFFF
PATCHES_DIRECTORY = Path(__file__).resolve().parents[2] / 'data' / 'monsterhunter' / 'patches'
MANIFEST_NAME = 'patches.json'


def entry(key: int, dest: int, data: bytes) -> bytes:
    """One `u32 key, u32 dest, u32 len` entry with its data padded to 4 bytes."""

    return _ENTRY_HEADER.pack(key, dest, len(data)) + data + bytes(-len(data) % 4)


def blob(magic: bytes, entries: Sequence[bytes]) -> bytes:
    """`magic[8], u32 count`, then the entries."""

    return _BLOB_HEADER.pack(magic, len(entries)) + b''.join(entries)


def resident_address(resident: int, offset: int = 0) -> int:
    """RAM address of byte `offset` of the first entry's data once the blob is resident at `resident`."""

    return resident + _BLOB_HEADER.size + _ENTRY_HEADER.size + offset


def text_blob(
    magic: bytes,
    resident: int,
    texts: Sequence[bytes],
    pokes: Callable[[list[int]], list[tuple[int, int, bytes]]],
    *,
    code: bytes = b'',
) -> bytes:
    """A blob whose first entry keeps `code` then `texts` (NUL-terminated) resident, followed by `pokes(addresses)`."""

    data = code + b''.join(text + b'\x00' for text in texts)
    addresses = []
    offset = len(code)
    for text in texts:
        addresses.append(resident_address(resident, offset))
        offset += len(text) + 1
    return blob(magic, [entry(RESIDENT_DATA_KEY, 0, data), *(entry(*poke) for poke in pokes(addresses))])


def write_patch(
    build: str,
    name: bytes,
    version: bytes,
    offered_to: Sequence[bytes],
    data: bytes,
    *,
    enabled: bool,
    changelog: Sequence[str],
) -> Path:
    """Write `<build>_<version>.bin` and its `patches.json` entry; return the blob path."""

    if len(name) != 10 or len(version) != 4:
        raise ValueError('a patch needs a 10-character name and a 4-character version')
    PATCHES_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = PATCHES_DIRECTORY / f'{build}_{version.decode("ascii")}.bin'
    path.write_bytes(data)
    manifest_path = PATCHES_DIRECTORY / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    previous = manifest.get(build, {}).get('file')
    if previous is not None and previous != path.name:
        (PATCHES_DIRECTORY / previous).unlink(missing_ok=True)
    manifest[build] = {
        'file': path.name,
        'name': name.decode('ascii'),
        'version': version.decode('ascii'),
        'offered_to': [stamp.decode('ascii') for stamp in offered_to],
        'enabled': enabled,
        'changelog': list(changelog),
    }
    manifest_path.write_text(json.dumps(dict(sorted(manifest.items())), indent=2) + '\n')
    return path
