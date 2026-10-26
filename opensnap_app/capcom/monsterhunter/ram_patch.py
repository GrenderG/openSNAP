"""RAM-only client patch delivery for retail Monster Hunter EU and NA.

The DNAS install path of `patch.py` needs a signature openSNAP cannot make,
so a stock client never accepts a downloaded patch.  This module delivers the
same `M-HUNTER` blob through the online APP session instead: one of the
client's own download handlers is used as an arbitrary RAM writer, a small
MIPS shellcode is planted in the resident patch buffer, and the client's own
`PatchExecCS` applier installs the blob's keys.  Nothing is written to the
memory card and nothing on the client is modified before the connection; a
new session starts from the unpatched client again.

Two routes, one per build
-------------------------
EU (`SLES_527.07`, below): the 6503 World list primes 6204's globals and 6204
writes 754-byte chunks to `0x2039e300`; the applier is `0x00248350`.
NA (`SLUS_208.96`): 6503 record 16 primes `[0x5529e1]` (name) and `[0x552a00]`
(write base) from its description, and 6002 writes a suffix of the 64 KiB
window at that base.  The World flow leaves 6002's guard (`0x00399654`) set, so
the same reply also primes 6204 (`0x6204`, which has no guard) to clear it;
after that 6002 uploads `0x203d9980` and writes the 6502 slot.  The applier is
`0x00258400`.  See `build_na_ram_patch` for the sequence and its limits.

The EU route, as validated on `SLES_527.07`
-------------------------------------------
6503 World list (handler `0x002a1e40`) stores each World in a 608-byte record
at `0x0050f030 + (cursor + i) * 608`; record 16 is `0x00511630`.  The handler
rejects `total >= 17` and pages at most 8 records per request, so record 16 is
reached in two steps: a normal `total = 16, start = 0, count = 8` page fills
records 0..7 and, because `start + count < total`, advances the client's cursor
to 8 and makes it ask for the next page; the server answers that request with
`total = W, start = 8, count = 9`, which fills records 8..15 and then record
16.  `W` is the real World count: the write index is the cursor (8), not
`total`, so a one-World list still reaches record 16, and the client's list
only contains the configured Worlds.  Record 16's fields cover the 6204
globals:

    field key (62) at +0x20    `0x00511650`: flag, index, offset, limit
    field name (15) at +0x5f   `0x0051168f`
    8 raw bytes at +0x6f       `0x0051169f`
    field desc (255) at +0x77  `0x005116a7`

The final page ends with `start + count = 17 >= total`, so the client stops
paging and runs its completion path.  That path resets the cursor, so the
handler page (the same 9-record shape at start 8) is preceded by a second
8-record prelude; record 16 is then written again for the handler write.

Record 16's description also runs over the APP receive header and body (EU
`0x005116c0`/`0x005116c8`, NA `0x00552a20`/`0x00552a28`).  The field decoders
(EU `0x0029e720`, NA `0x002ae5b0`) copy a field to its destination and only then
read the frame header's sequence byte from that destination to key the field
checksum.  For a description that covers the header, the copy has just
overwritten that byte, so the description encodes the header's sequence slot to
its own sequence; the rest of the header and the receive body are carried
unchanged (`_priming_sequence_byte`).  Without this, the client reads a masked
byte, computes a different checksum and closes the APP connection.

The download handler 6204 (`0x0029f920`) then writes a field to
`[gp+620] + offset`, where `[gp+620] = 0x00a7a200` (`0x00110964`).  Its array
of end offsets is at `0x0051169c`; index 1 is `0x005116a0`:
`offset + 754 < array[1]` writes 754 bytes and advances the offset, otherwise
the remainder is written, the index advances, and index >= limit finishes the
download.  Priming index 1 with `array[1] = offset + size` therefore uploads
one contiguous payload in 754-byte chunks.

The payload is written to the uncached RAM mirror `0x2039e300`, which is the
resident patch buffer `0x0039e300` seen through `0x20000000`.  The shellcode
follows the blob there and is entered through a second 6204 write into the
dispatcher's 6502 reply handler (`0x0032ac0c`, normally `0x002a2420`).  The
burst then ends by pushing the 6502 reply itself (sequence 0): the client
dispatches by command, not sequence, so the shellcode runs as soon as the
frame is reached, with no round trip to exceed the client's ~235 ms
world-status timeout (D901).

The shellcode copies the blob through the cached address (so the applier does
not read stale D-cache lines), writes the `M-HUNTER` header (`0x003ae300`,
length at `0x003ae314`), calls the applier for key 0 and for the loaded
overlays, flushes the caches, restores the handler table and tail-calls the
real 6502 handler.  It preserves `ra`, `a0`, `sp`, `gp`, `s0` and `s1`.

This works for a World list of at most 16 Worlds (record 16 must stay outside
the real list); larger lists keep the normal 6503 reply and are not patched.
The client must be retail EU, not the EU demo (`0412241846`) or the TLES test
build, whose patch addresses differ.

It also works when the client already carries Capcom's official 1001 patch
(`0509261742`): the burst overwrites that patch's resident blob and header, and
`eu_custom_1002.bin` is built on the same code. Either way the applier copies
our header name to the runtime version, which the next 1002 answer reports as
`M-HUNTER`, so the service does not patch that game process again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import struct
from pathlib import Path

from opensnap_app.capcom.monsterhunter.constants import (
    FINAL_PAGE_COUNT,
    FINAL_PAGE_START,
    WORLD_PAGE_SIZE,
    WORLD_STATUS_REPLY,
)
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_REPLY,
    APP_FIELD_MASK,
    app_frame,
    encode_app_field,
)

# The APP client reads every frame body into a 768-byte buffer (EU
# `[0x0050ed14]`, NA `[0x00550048]`), so a 6503 page delivered in a reply must
# not exceed this.  Larger World lists therefore keep the normal paged reply.
APP_BODY_LIMIT = 768

# `PatchExecCS` and its resident buffers (`patch.py`).
BLOB_MAGIC = b'M-HUNTER'
RESIDENT_BLOB = 0x0039E300
HEADER = 0x003AE300
HEADER_LENGTH = 0x14
RESIDENT_SIZE = 0x10000
APPLIER = 0x00248350
OVERLAY_ID = 0x00216F50
FLUSH_CACHE = 0x002177A0
# 6502 reply handler entry (direction 2); the dispatcher table is `0x0032ab90`.
HANDLER_6502_REPLY = 0x0032AC0C
HANDLER_6502 = 0x002A2420
# 6204's destination base (`[gp+620]`, set by `0x00110964`).
WRITE_BASE = 0x00A7A200
# 6503 record 16 and the 6204 globals it covers.
WORLD_RECORD_BASE = 0x0050F030
WORLD_RECORD_SIZE = 608
PRIMING_RECORD = 16
DOWNLOAD_INDEX = 1
DOWNLOAD_LIMIT = 2
CHUNK_SIZE = 754
# The APP command of the 6204 download handler (`CMD_APP_EVENT_DOWNLOAD`).
CMD_APP_EVENT_DOWNLOAD = 0x6204
CMD_APP_PATCH_CHUNK = 0x6002
CMD_APP_WORLD_LIST = 0x6503
CMD_APP_WORLD_STATUS = 0x6502
TOTAL_RECORDS = PRIMING_RECORD
# The EU demo (`SLED_530.83`) uses different patch addresses.
EU_DEMO_VERSION = b'0412241846'
# The 10-byte name 6002 compares against `0x005529e1`, and the applier copies
# into the runtime version (10 bytes), so the next 1002 answer is `M-HUNTER`
# and the service does not patch this process again.
NA_NAME = BLOB_MAGIC + b'\x00\x00'
NA_RESIDENT_BLOB = 0x003D9980
NA_HEADER = 0x003E9980
NA_HEADER_LENGTH = 0x14
NA_RESIDENT_SIZE = 0x10000
NA_APPLIER = 0x00258400
NA_OVERLAY_ID = 0x00220EA0
NA_FLUSH_CACHE = 0x00221660
# The 6502 reply handler slot (direction 2, index 33 in the table `0x00369a30`).
NA_HANDLER_6502_REPLY = 0x00369AB4
NA_HANDLER_6502 = 0x002B2270
# `[0x00552a00]`: base of every 6002 write, primed through 6503 record 16.
NA_WRITE_BASE = 0x00552A00
# 6204 writes `data` at `[gp-18716] + offset`; the init (`0x001108d4`) fixes
# that pointer at `0x00a7a200`.  Unlike 6002 it has no guard, so the 6503 reply
# uses it once to clear the guard and enable 6002.
NA_DOWNLOAD_BASE = 0x00A7A200
NA_DOWNLOAD_INDEX = 1
# Record 16's key field (`0x00552950`) is also the 1002 login block: the reply
# builder (`0x002aeb40`) reads a user-ID pointer, two flags and a version-string
# pointer from it.  Its init (`0x00247fc4`) uses fixed values, and 6503's key
# zero-fill would otherwise wipe them (the next connection then sends an empty
# 1002 body).
NA_LOGIN_BLOCK = (
    struct.pack('<I', 0x00541130)
    + bytes((2, 1, 0, 0))
    + struct.pack('<I', 0x003A2760)
    + bytes(4)
)
# Record 16's name field (`0x0055298f`) overlaps 6204's expected index
# (`0x00552990`) and expected offset (`0x00552994`).
NA_RECORD_NAME = 0x0055298F
NA_DOWNLOAD_INDEX_OFFSET = 0x00552990 - NA_RECORD_NAME
NA_DOWNLOAD_OFFSET_OFFSET = 0x00552994 - NA_RECORD_NAME
# Record 16's description (`0x005529a7`) overlaps 6204's end-offset table
# (`0x005529bc`); `NA_DOWNLOAD_INDEX`'s entry is four bytes in.
NA_DESCRIPTION_TABLE = 0x005529BC - 0x005529A7
# `[0x00399654]`: 6002 only writes while this byte is 0.
NA_GUARD = 0x00399654
# 6204's base is `0x00a7a200`, so this offset wraps to the guard.
NA_GUARD_OFFSET = (NA_GUARD - NA_DOWNLOAD_BASE) & 0xFFFFFFFF
# 6503 record 16 (`0x00552930`) and its description field (`0x005529a7`).
NA_WORLD_RECORD = 0x00552930
NA_RECORD_DESCRIPTION = 0x005529A7
# Offsets inside that 255-byte description.  The 6001 handler normally fills
# the same globals from `field name[10], ver[4], u32 size, u32`; record 16 is
# exactly the length of that prefix plus the padding the handler skips.
NA_DESCRIPTION_NAME = 0x005529E1
NA_DESCRIPTION_VERSION = 0x005529F1
NA_DESCRIPTION_SIZE = 0x005529FC
# The 6503 World record array ends at `0x511630`; record 16 is the download
# globals area.  Its description is 255 bytes and reaches the frame header
# buffer at `0x5116c0`, so the description must carry those bytes unchanged.
EU_DESCRIPTION_FIELD_SIZE = 255
EU_DESCRIPTION_FRAME_HEADER = 0x005116C0 - 0x005116A7
# The receive body starts right after the receive header (`0x005116C8`); the
# description reaches it too and must not be decoded as a stale body.
EU_DESCRIPTION_BODY = 0x005116C8 - 0x005116A7
# NA record 16's description reaches the frame header (`0x552a20`) and the
# receive body (`0x552a28`); carry both unchanged in the 255-byte description.
NA_DESCRIPTION_FIELD_SIZE = 255
NA_DESCRIPTION_FRAME_HEADER = 0x00552A20 - NA_RECORD_DESCRIPTION
NA_DESCRIPTION_BODY = 0x00552A28 - NA_RECORD_DESCRIPTION
NA_DESCRIPTION_BODY_SIZE = NA_DESCRIPTION_FIELD_SIZE - NA_DESCRIPTION_BODY
NA_DESCRIPTION_LENGTH = NA_WRITE_BASE + 4 - NA_RECORD_DESCRIPTION
# 6002 writes `data` at `[0x00552a00] + offset` and then zero-fills the rest
# of the enclosing 64 KiB window.  A write is therefore a suffix of the
# window: offset `0x10000 - len(data)` and a base chosen so the window ends
# where the write must end.
NA_WINDOW = 0x10000
# Below both the client memcpy's 32-byte `lq`/`sq` threshold and a 16-byte
# boundary, so both its copy and its zero-fill stay byte-wise. The client's
# 128-bit `sq`/`lq` must not be used on the uncached mirror.
NA_CHUNK_SIZE = 31
# The payload and shellcode are written through the uncached mirror of the
# resident buffer; the shellcode is entered through the handler table, so it
# runs uncached and needs no I-cache flush to start.
NA_UNCACHED_BLOB = 0x20000000 + NA_RESIDENT_BLOB

# Offsets of the globals inside the priming record (record 16 at `0x00511630`).
_PRIMING_FLAG = 0x00511650
_PRIMING_INDEX = 0x00511651
_PRIMING_OFFSET = 0x00511654
_PRIMING_LIMIT = 0x0051165A
_PRIMING_ARRAY = 0x0051169C
_PRIMING_RAW = 0x0051169F
_PRIMING_END = _PRIMING_ARRAY + DOWNLOAD_INDEX * 4

# MIPS registers.
_ZERO, _V0, _V1, _A0, _A1 = 0, 2, 3, 4, 5
_T0, _T1, _T2, _T3 = 8, 9, 10, 11
_SP, _RA = 29, 31

# The shellcode is written to the uncached mirror and entered there; the
# handler table holds its uncached address so the CPU does not need an I-cache
# flush to start it.
_UNCACHED_MIRROR = 0x20000000


@dataclass(frozen=True, slots=True)
class PatchEntry:
    """One entry of a `M-HUNTER` blob (little-endian)."""

    key: int
    destination: int
    length: int
    # `data` for an inline entry, or the absolute source address for a copy.
    data: bytes
    source: int | None = None


@dataclass(frozen=True, slots=True)
class PatchBlob:
    """A parsed `M-HUNTER` blob."""

    raw: bytes
    entries: tuple[PatchEntry, ...]


def parse_patch_blob(data: bytes) -> PatchBlob:
    """Validate a raw `M-HUNTER` blob and return its entries.

    The applier reads `count` entries, each `u32 key, u32 dest, u32 len`
    (`len` bit 31 = copy from the `u32` source that follows, otherwise `len`
    inline bytes padded to 4) and ignores every byte after the last entry.
    Capcom's official EU blob keeps the replacement code that its hooks jump
    to in that trailing area, so it is accepted as well.
    """

    if len(data) < 12 or data[:8] != BLOB_MAGIC:
        raise ValueError('the RAM patch blob is not a retail M-HUNTER patch')
    count = struct.unpack_from('<I', data, 8)[0]
    if not 1 <= count <= RESIDENT_SIZE // 12:
        raise ValueError('the RAM patch blob has an impossible entry count')
    offset = 12
    entries: list[PatchEntry] = []
    for _ in range(count):
        if offset + 12 > len(data):
            raise ValueError('the RAM patch blob is truncated')
        key, destination, length = struct.unpack_from('<III', data, offset)
        offset += 12
        source = None
        if length & 0x80000000:
            if offset + 4 > len(data):
                raise ValueError('the RAM patch blob is truncated')
            source = struct.unpack_from('<I', data, offset)[0]
            offset += 4
            entry_data = b''
            size = length & 0x7FFFFFFF
        else:
            size = length
            if offset + size > len(data):
                raise ValueError('the RAM patch blob is truncated')
            entry_data = data[offset:offset + size]
            offset += size
        offset = (offset + 3) & ~3
        if offset > len(data):
            raise ValueError('the RAM patch blob is truncated')
        entries.append(PatchEntry(key, destination, size, entry_data, source))
    return PatchBlob(data, tuple(entries))


def load_ram_patch(path: str | Path, *, environment_key: str) -> bytes:
    """Read and validate the blob named by `environment_key`.

    Every failure is a `ValueError` so the CAPCOM APP entrypoint leaves just
    this title out (with the reason logged) instead of aborting.
    """

    try:
        blob = Path(path).read_bytes()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ValueError(f'{environment_key}: cannot read {path}: {reason}') from None
    try:
        parse_patch_blob(blob)
    except ValueError as exc:
        raise ValueError(f'{environment_key}: {path}: {exc}') from None
    # The helper follows the blob in the same 64 KiB resident buffer.  The EU
    # helper is the longer of the two, so this bound is safe for both routes.
    if align(len(blob), 16) + len(build_shellcode(len(blob))) > RESIDENT_SIZE:
        raise ValueError(
            f'{environment_key}: {path}: the patch and its helper do not fit '
            f'the {RESIDENT_SIZE} byte resident buffer'
        )
    return blob


@dataclass(frozen=True, slots=True)
class RamPatchPlan:
    """The World list reply that starts a RAM patch burst and the frames after it."""

    reply: bytes
    frames: tuple[bytes, ...]
    shellcode_address: int
    payload_size: int
    # 'eu' (`SLES_527.07`) or 'na' (`SLUS_208.96`).
    variant: str = 'eu'
    # Client requests that arrive because of the burst and must stay
    # unanswered, so the client keeps the state the next injected frame needs.
    ignore_6502: int = 0
    ignore_6503: int = 0
    ignore_6211: int = 0
    ignore_6403: int = 0
    ignore_6002: int = 0
    # First 8-record page: the client asks for the next page at start 8.
    prelude: bytes | None = None
    # This reply is only the prelude; wait for the next 6503 request.
    wait_for_next: bool = False


class RamPatchPageTooLarge(ValueError):
    """A priming 6503 page exceeds the client's 768-byte frame body."""


def _ensure_body_size(*payloads: bytes) -> None:
    """Reject a World page the client's APP receive buffer cannot hold."""

    for payload in payloads:
        if len(payload) > APP_BODY_LIMIT:
            raise RamPatchPageTooLarge(
                f'the RAM patch World page is {len(payload)} bytes; '
                f'the client accepts at most {APP_BODY_LIMIT}'
            )


def _world_status_frame() -> bytes:
    """The pushed 6502 reply that ends a burst and runs the armed shellcode."""

    return app_frame(
        APP_DIRECTION_REPLY, CMD_APP_WORLD_STATUS, sequence=0, payload=WORLD_STATUS_REPLY
    )


def build_ram_patch(
    blob: bytes,
    worlds: Sequence[tuple[str, str, str]],
    *,
    sequence: int,
) -> RamPatchPlan:
    """Build the two World-list pages and the injection burst for `blob`.

    `worlds` are `(key, name, description)` of the configured Worlds, in menu
    order.  `sequence` is the client's first 6503 request sequence: the
    8-record prelude uses it.  The client then asks for the page at start 8
    with its own next sequence; the caller builds that page with that sequence.
    """

    parse_patch_blob(blob)
    if not 1 <= len(worlds) <= 16:
        raise ValueError('RAM patches need between 1 and 16 Worlds')
    shellcode = build_shellcode(len(blob))
    shellcode_offset = align(len(blob), 16)
    payload = blob + bytes(shellcode_offset - len(blob)) + shellcode
    if shellcode_offset + len(shellcode) > RESIDENT_SIZE:
        raise ValueError('the RAM patch does not fit in the resident patch buffer')
    shellcode_address = (_UNCACHED_MIRROR + RESIDENT_BLOB + shellcode_offset) & 0xFFFFFFFF

    # First page: a normal 8-record page ending before record 16.  Its
    # completion advances the client's record cursor to 8 and sends the next
    # 6503 request (start 8).
    prelude = _world_page(
        worlds,
        sequence=sequence,
        total=TOTAL_RECORDS,
        start=0,
        count=WORLD_PAGE_SIZE,
    )
    # Final page: 8 records from 8, then record 16.  The client's cursor is 8,
    # so the ninth record is index 16; it primes the payload upload.
    reply = _world_page(
        worlds,
        sequence=sequence,
        total=len(worlds),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
        prime=_Prime(payload, X=offset_to(_UNCACHED_MIRROR + RESIDENT_BLOB)),
    )
    # Handler page: the same 9-record shape, with record 16 priming the handler
    # write.  A final page resets the cursor, so another prelude must restore 8
    # before this page.
    handler_page = _world_page(
        worlds,
        sequence=0,
        total=len(worlds),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
        prime=_Prime(struct.pack('<I', shellcode_address), X=handler_offset()),
    )
    page_prelude = _world_page(
        worlds,
        sequence=0,
        total=TOTAL_RECORDS,
        start=0,
        count=WORLD_PAGE_SIZE,
    )
    _ensure_body_size(prelude, reply, handler_page, page_prelude)

    frames: list[bytes] = list(_upload_frames(payload, _UNCACHED_MIRROR + RESIDENT_BLOB))
    # The payload download completes into state 20; the prelude then puts the
    # World cursor back at 8 before the handler page primes the handler write.
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=page_prelude)
    )
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=handler_page)
    )
    frames.append(
        app_frame(
            APP_DIRECTION_REPLY,
            CMD_APP_EVENT_DOWNLOAD,
            sequence=0,
            payload=_download_payload(handler_offset(), struct.pack('<I', shellcode_address)),
        )
    )
    # The pushed 6502 reply runs the armed shellcode.
    frames.append(_world_status_frame())
    return RamPatchPlan(
        reply,
        tuple(frames),
        shellcode_address,
        len(payload),
        variant='eu',
        # Two 6502s (final page, handler page), answered by the pushed reply.
        ignore_6502=2,
        # The injected prelude makes the list ask for the next page.
        ignore_6503=1,
        # One 6211 for the payload download, one for the handler write.
        ignore_6211=2,
        prelude=prelude,
    )


def build_na_ram_patch(
    blob: bytes,
    worlds: Sequence[tuple[str, str, str]],
    *,
    sequence: int,
) -> RamPatchPlan:
    """Build the NA burst for `blob` (`SLUS_208.96`, stock `0407251750`).

    NA's 6002 patch handler is an arbitrary writer while its guard
    (`0x00399654`) is 0, but the World list's connection init leaves that guard
    at 1 (the value `0x00399660` holds from `0x0024a590`).  6204
    (`0x002afc70`) has no guard and writes `data` at `[0x00399354] + offset`,
    where the init (`0x001108d4`) fixes the pointer at `0x00a7a200`; record
    16's name field overlaps 6204's expected index (`0x00552990`) and offset
    (`0x00552994`), and its description overlaps 6204's end-offset table
    (`0x005529bc`).  The first reply therefore primes both handlers.

    The sequence is:

    1. prelude page, then the final page primes 6002 to upload at
       `base = payload_end - 0x10000` and 6204 to write one byte to the guard;
       its key field also restores the 1002 login block that the 6503 handler
       zero-fills (`0x00552950`), so the next connection keeps its login;
    2. a 6204 frame clears the guard, then 6002 uploads `blob`, padding and
       shellcode to `0x203d9980` in 31-byte window suffixes;
    3. another prelude restores the World cursor to 8 (the first final page
       completed with the guard set and reset it), then a final page re-primes
       `base = 0x369ab4 - 0xfffc` and a 6002 frame writes the shellcode address
       over the 6502 reply handler slot;
    4. a final page re-primes `base = 0x399654 - 0xffff`, a 6002 frame writes
       guard byte 2, and the last final page completes into state 8.  The
       burst then ends by pushing the 6502 reply itself, so the armed
       shellcode runs without a round trip; the client's own 6502 requests
       (from the first final page and this trigger page) stay unanswered.

    With the guard clear, the handler and guard pages complete into state 54
    and keep the cursor at 8; their 6403 requests are ignored, as are the 6002
    requests and
    the Market request from the guard-clearing 6204.  The client must be the
    stock build: the public beta and the TLES test build use different handler
    addresses.
    """

    parse_patch_blob(blob)
    if not 1 <= len(worlds) <= 16:
        raise ValueError('RAM patches need between 1 and 16 Worlds')
    shellcode = build_na_shellcode(len(blob))
    shellcode_offset = align(len(blob), 16)
    payload = blob + bytes(shellcode_offset - len(blob)) + shellcode
    if shellcode_offset + len(shellcode) > NA_RESIDENT_SIZE:
        raise ValueError('the RAM patch does not fit in the resident patch buffer')
    # Every upload write ends at `payload_end`; the base is the corresponding
    # window start.  Ascending chunks only zero bytes not written yet.
    upload_base = (NA_UNCACHED_BLOB + len(payload) - NA_WINDOW) & 0xFFFFFFFF
    shellcode_address = (NA_UNCACHED_BLOB + shellcode_offset) & 0xFFFFFFFF

    reply = _na_page(
        worlds,
        sequence=sequence,
        total=len(worlds),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
        base=upload_base,
        size=len(payload),
        name=_na_guard_bootstrap_name(),
        bootstrap=NA_GUARD_OFFSET,
    )
    prelude = _na_page(
        worlds,
        sequence=sequence,
        total=TOTAL_RECORDS,
        start=0,
        count=WORLD_PAGE_SIZE,
    )
    handler_base = (NA_HANDLER_6502_REPLY - (NA_WINDOW - 4)) & 0xFFFFFFFF
    guard_base = (NA_GUARD - (NA_WINDOW - 1)) & 0xFFFFFFFF
    handler_page = _na_page(
        worlds,
        sequence=0,
        total=len(worlds),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
        base=handler_base,
        size=len(payload),
    )
    guard_page = _na_page(
        worlds,
        sequence=0,
        total=len(worlds),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
        base=guard_base,
        size=len(payload),
    )
    # The guard-clear write leaves state 20, so the cursor is still 8 only
    # after a prelude.  The trigger page completes with guard 2, choosing
    # state 8.
    trigger_page = guard_page
    page_prelude = _na_page(
        worlds,
        sequence=0,
        total=TOTAL_RECORDS,
        start=0,
        count=WORLD_PAGE_SIZE,
    )
    _ensure_body_size(prelude, reply, handler_page, guard_page, trigger_page, page_prelude)

    uploads = _na_upload_frames(payload, upload_base)
    frames: list[bytes] = [
        # 6002 is gated by `0x00399654`; the World flow leaves it 1.  The 6204
        # handler has no guard, so one byte there clears it first.
        app_frame(
            APP_DIRECTION_REPLY,
            CMD_APP_EVENT_DOWNLOAD,
            sequence=0,
            payload=_download_payload(NA_GUARD_OFFSET, b'\x00'),
        )
    ]
    frames.extend(uploads)
    # A final page completes with the guard clear, so the cursor keeps its 8;
    # the prelude is only needed because the first final page reset it to 0.
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=page_prelude)
    )
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=handler_page)
    )
    frames.append(_na_patch_frame(0xFFFC, struct.pack('<I', shellcode_address)))
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=guard_page)
    )
    frames.append(_na_patch_frame(0xFFFF, b'\x02'))
    frames.append(
        app_frame(APP_DIRECTION_REPLY, CMD_APP_WORLD_LIST, sequence=0, payload=trigger_page)
    )
    # The pushed 6502 reply runs the armed shellcode.
    frames.append(_world_status_frame())
    return RamPatchPlan(
        reply,
        tuple(frames),
        shellcode_address,
        len(payload),
        variant='na',
        # Two 6502s (first final page, trigger page), answered by the pushed reply.
        ignore_6502=2,
        ignore_6503=1,
        # The guard-clear 6204 completes into state 20, which asks for the Market.
        ignore_6211=1,
        # The handler and guard pages complete with the guard clear and ask
        # for the Land list.
        ignore_6403=2,
        # Every injected 6002 (uploads, handler write, guard write) leaves the
        # client in state 23, which asks for the next chunk.
        ignore_6002=len(uploads) + 2,
        prelude=prelude,
    )


def _na_guard_bootstrap_name() -> bytes:
    """Record 16's 15-byte name field that primes the guard-clearing 6204.

    `0x00552990` is 6204's expected index and `0x00552994` its expected
    offset; both live inside record 16's name field (maximum 15 bytes).
    """

    name = bytearray(15)
    name[NA_DOWNLOAD_INDEX_OFFSET] = NA_DOWNLOAD_INDEX
    struct.pack_into('<I', name, NA_DOWNLOAD_OFFSET_OFFSET, NA_GUARD_OFFSET)
    return bytes(name)


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def offset_to(address: int) -> int:
    """The 6204 offset that writes `address` (`0x00a7a200 + offset`)."""

    return (address - WRITE_BASE) & 0xFFFFFFFF


def handler_offset() -> int:
    return offset_to(HANDLER_6502_REPLY)


@dataclass(frozen=True, slots=True)
class _Prime:
    """One priming record: where a download starts and where it ends."""

    payload: bytes
    X: int

    @property
    def end(self) -> int:
        return (self.X + len(self.payload)) & 0xFFFFFFFF


def _upload_frames(payload: bytes, address: int) -> list[bytes]:
    """The 6204 frames that write `payload` at `address` in 754-byte chunks."""

    offset = offset_to(address)
    frames = []
    position = 0
    while position + CHUNK_SIZE < len(payload):
        frames.append(
            app_frame(
                APP_DIRECTION_REPLY,
                CMD_APP_EVENT_DOWNLOAD,
                sequence=0,
                payload=_download_payload(
                    offset + position, payload[position:position + CHUNK_SIZE]
                ),
            )
        )
        position += CHUNK_SIZE
    frames.append(
        app_frame(
            APP_DIRECTION_REPLY,
            CMD_APP_EVENT_DOWNLOAD,
            sequence=0,
            payload=_download_payload(offset + position, payload[position:]),
        )
    )
    return frames


def _download_payload(offset: int, data: bytes) -> bytes:
    """6204: `u16 index, u32 offset, field data` (both integers big-endian)."""

    return struct.pack('>HL', DOWNLOAD_INDEX, offset) + encode_app_field(data, 0)


def _frame_header(payload: bytes, sequence: int) -> bytes:
    """The 8-byte APP frame header for `payload` (same bytes `app_frame` writes)."""

    return (
        len(payload).to_bytes(2, 'big')
        + bytes([APP_DIRECTION_REPLY])
        + CMD_APP_WORLD_LIST.to_bytes(2, 'big')
        + bytes([sequence & 0xFF, 0, 0xFF])
    )


def _world_page(
    worlds: Sequence[tuple[str, str, str]],
    *,
    sequence: int,
    total: int,
    start: int,
    count: int,
    prime: _Prime | None = None,
) -> bytes:
    """One 6503 World-list page, optionally ending at record 16."""

    def field(data: bytes) -> bytes:
        return encode_app_field(data, sequence)

    def empty() -> bytes:
        return field(b'') + field(b'') + bytes(8) + field(b'')

    records = []
    for index in range(start, start + count):
        if index < PRIMING_RECORD and index < len(worlds):
            key, name, description = worlds[index]
            records.append(
                field(key.encode('ascii'))
                + field(name.encode('ascii'))
                + bytes(8)
                + field(description.encode('ascii'))
            )
        else:
            records.append(empty())
    header = struct.pack('>2HB', total, start, count)
    if prime is None or not start <= PRIMING_RECORD < start + count:
        return header + b''.join(records)
    prefix = header + b''.join(records[:-1]) + _priming_fields(prime, sequence)
    frame_header = _frame_header(prefix + bytes(4 + EU_DESCRIPTION_FIELD_SIZE), sequence)
    body = prefix[:EU_DESCRIPTION_FIELD_SIZE - EU_DESCRIPTION_BODY]
    body += bytes(EU_DESCRIPTION_FIELD_SIZE - EU_DESCRIPTION_BODY - len(body))
    return prefix + encode_app_field(
        _eu_priming_description(frame_header, body, sequence), sequence
    )


def _priming_fields(prime: _Prime, sequence: int) -> bytes:
    """Record 16's key, empty name and 8 raw bytes, before its description."""

    # 0x00511650: flag 0, index 1, 2 bytes padding, offset, 2 bytes padding, limit.
    key = (
        bytes((0, DOWNLOAD_INDEX, 0, 0))
        + struct.pack('<I', prime.X)
        + b'\x00\x00'
        + struct.pack('<H', DOWNLOAD_LIMIT)
    )
    # 0x0051169f: array[0]'s high byte, array[1] = end, array[2]'s low bytes.
    raw = b'\x00' + struct.pack('<I', prime.end) + b'\x00\x00\x00'
    return encode_app_field(key, sequence) + encode_app_field(b'', sequence) + raw


def _priming_sequence_byte(sequence: int, offset: int) -> int:
    """Plaintext byte the field encoder turns into `sequence` at `offset`.

    The field decoders (EU `0x0029e720`, NA `0x002ae5b0`) copy a field into its
    destination and then read the frame header's sequence byte from that
    destination to key the field checksum.  A priming description covers the
    receive header, so the copy overwrites the sequence byte it is about to
    read.  Putting this value at the header's sequence offset makes the encoded
    byte there equal `sequence`, so the decoder uses the same key the server
    used and the checksum matches.  The decoded byte at that offset is this
    value rather than the sequence, but the description is the last field and
    the receive buffer is rewritten by the next frame.
    """

    mask = APP_FIELD_MASK[offset & 7]
    return (sequence ^ mask ^ ((sequence + offset) & 0xFF)) & 0xFF


def _eu_priming_description(frame_header: bytes, body: bytes, sequence: int) -> bytes:
    """Record 16's description, preserving the frame header it overwrites.

    Record 16 starts at `0x511630`; its description at `0x5116a7` reaches the
    frame header buffer at `0x5116c0` and the receive body at `0x5116c8`.
    `body` is the received page prefix, padded to the end of the description.
    """

    data = bytearray(bytes(EU_DESCRIPTION_FRAME_HEADER) + frame_header + body)
    if len(data) != EU_DESCRIPTION_FIELD_SIZE:
        raise ValueError('the EU priming description is misaligned')
    offset = EU_DESCRIPTION_FRAME_HEADER + 5
    data[offset] = _priming_sequence_byte(sequence, offset)
    return bytes(data)


def _na_page(
    worlds: Sequence[tuple[str, str, str]],
    *,
    sequence: int,
    total: int,
    start: int,
    count: int,
    base: int | None = None,
    size: int = 0,
    name: bytes = b'',
    bootstrap: int | None = None,
) -> bytes:
    """One NA 6503 World-list page, optionally ending at record 16."""

    def field(data: bytes) -> bytes:
        return encode_app_field(data, sequence)

    records = []
    for index in range(start, start + count):
        if index < PRIMING_RECORD and index < len(worlds):
            key, world_name, description = worlds[index]
            records.append(
                field(key.encode('ascii'))
                + field(world_name.encode('ascii'))
                + bytes(8)
                + field(description.encode('ascii'))
            )
        else:
            records.append(field(b'') + field(b'') + bytes(8) + field(b''))
    header = struct.pack('>2HB', total, start, count)
    if base is None or not start <= PRIMING_RECORD < start + count:
        return header + b''.join(records)
    record_name = (name + bytes(15))[:15]
    prefix = (
        header + b''.join(records[:-1]) + field(NA_LOGIN_BLOCK) + field(record_name) + bytes(8)
    )
    frame_header = _frame_header(prefix + bytes(4 + NA_DESCRIPTION_FIELD_SIZE), sequence)
    description = _na_priming_description(
        base=base,
        size=size,
        frame_header=frame_header,
        body_prefix=prefix[:NA_DESCRIPTION_BODY_SIZE],
        sequence=sequence,
        bootstrap=bootstrap,
    )
    return prefix + encode_app_field(description, sequence)


def _na_priming_description(
    *,
    base: int,
    size: int,
    frame_header: bytes,
    body_prefix: bytes,
    sequence: int,
    bootstrap: int | None,
) -> bytes:
    """The description of record 16, laid out over `0x005529e0`..`0x00552a03`.

    `0x005529e0` is the flag 6001 writes on APP1, `0x005529e1` the name 6002
    compares, `0x005529f1` the display version, `0x005529fc` the patch size and
    `0x00552a00` the write base.  Everything between them is padding the 6001
    handler also skips.  The description also covers 6204's end-offset table at
    `0x005529bc` (`bootstrap`) and continues to `0x00552aa5`, over the frame
    header at `0x00552a20` and the receive body at `0x00552a28`; carrying their
    original bytes keeps the overwrite harmless, except for the header's
    sequence byte, which must encode to the field's own sequence.
    """

    data = (
        # 57 bytes of padding and `0x005529e0 = 0` (the "no patch" flag).
        bytes(NA_DESCRIPTION_NAME - NA_RECORD_DESCRIPTION)
        + NA_NAME
        + bytes(NA_DESCRIPTION_VERSION - NA_DESCRIPTION_NAME - len(NA_NAME))
        + b'1002'
        + bytes(NA_DESCRIPTION_SIZE - NA_DESCRIPTION_VERSION - 4)
        + struct.pack('<I', size)
        + bytes(NA_WRITE_BASE - NA_DESCRIPTION_SIZE - 4)
        + struct.pack('<I', base & 0xFFFFFFFF)
    )
    data += bytes(NA_DESCRIPTION_FRAME_HEADER - len(data))
    data += frame_header
    data += body_prefix
    if len(data) != NA_DESCRIPTION_FIELD_SIZE:
        raise ValueError('the NA priming description is misaligned')
    data = bytearray(data)
    if bootstrap is not None:
        struct.pack_into(
            '<I',
            data,
            NA_DESCRIPTION_TABLE + NA_DOWNLOAD_INDEX * 4,
            (bootstrap + 1) & 0xFFFFFFFF,
        )
    offset = NA_DESCRIPTION_FRAME_HEADER + 5
    data[offset] = _priming_sequence_byte(sequence, offset)
    return bytes(data)


def _na_patch_frame(offset: int, data: bytes) -> bytes:
    """6002: `field name[10], u32 offset, field data` (the offset is big-endian)."""

    return app_frame(
        APP_DIRECTION_REPLY,
        CMD_APP_PATCH_CHUNK,
        sequence=0,
        payload=(
            encode_app_field(NA_NAME, 0)
            + struct.pack('>I', offset)
            + encode_app_field(data, 0)
        ),
    )


def _na_upload_frames(payload: bytes, base: int) -> list[bytes]:
    """The 6002 frames that upload `payload` to the resident buffer.

    Each frame writes one chunk at `base + offset`, where the offset makes the
    write end at `0x203d9980 + len(payload)`.  The handler zero-fills up to
    that end, so ascending chunks only clear bytes a later chunk overwrites.
    """

    frames = []
    for position in range(0, len(payload), NA_CHUNK_SIZE):
        chunk = payload[position:position + NA_CHUNK_SIZE]
        offset = (NA_WINDOW - len(payload) + position) & 0xFFFFFFFF
        if not 0 <= offset < NA_WINDOW:
            raise ValueError('the RAM patch does not fit in one 6002 window')
        frames.append(_na_patch_frame(offset, chunk))
    return frames


class _Assembler:
    """Tiny MIPS III/R5900 encoder for the shellcode; branches resolve by label."""

    def __init__(self) -> None:
        self._words: list[int] = []
        self._labels: dict[str, int] = {}
        self._fixups: list[tuple[int, str, int, int]] = []

    def label(self, name: str) -> None:
        if name in self._labels:
            raise ValueError(f'duplicate label {name}')
        self._labels[name] = len(self._words) * 4

    def word(self, value: int) -> None:
        self._words.append(value & 0xFFFFFFFF)

    def lui(self, rt: int, immediate: int) -> None:
        self.word((0x0F << 26) | (rt << 16) | (immediate & 0xFFFF))

    def ori(self, rt: int, rs: int, immediate: int) -> None:
        self.word((0x0D << 26) | (rs << 21) | (rt << 16) | (immediate & 0xFFFF))

    def addiu(self, rt: int, rs: int, immediate: int) -> None:
        self.word((0x09 << 26) | (rs << 21) | (rt << 16) | (immediate & 0xFFFF))

    def li(self, rt: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.lui(rt, value >> 16)
        self.ori(rt, rt, value & 0xFFFF)

    def addu(self, rd: int, rs: int, rt: int) -> None:
        self.word((rs << 21) | (rt << 16) | (rd << 11) | 0x21)

    def move(self, rd: int, rs: int) -> None:
        self.addu(rd, rs, _ZERO)

    def jalr(self, rs: int) -> None:
        self.word((rs << 21) | (_RA << 11) | 0x09)

    def jr(self, rs: int) -> None:
        self.word((rs << 21) | 0x08)

    def lw(self, rt: int, offset: int, base: int) -> None:
        self.word((0x23 << 26) | (base << 21) | (rt << 16) | (offset & 0xFFFF))

    def sw(self, rt: int, offset: int, base: int) -> None:
        self.word((0x2B << 26) | (base << 21) | (rt << 16) | (offset & 0xFFFF))

    def ld(self, rt: int, offset: int, base: int) -> None:
        self.word((0x37 << 26) | (base << 21) | (rt << 16) | (offset & 0xFFFF))

    def sd(self, rt: int, offset: int, base: int) -> None:
        self.word((0x3F << 26) | (base << 21) | (rt << 16) | (offset & 0xFFFF))

    def nop(self) -> None:
        self.word(0)

    def bne(self, rs: int, rt: int, label: str) -> None:
        self._fixups.append((len(self._words), label, rs, rt))
        self.word(0)

    def beq(self, rs: int, rt: int, label: str) -> None:
        self._fixups.append((len(self._words), label, rs, rt))
        self.word((0x04 << 26))

    def encode(self) -> bytes:
        for index, label, rs, rt in self._fixups:
            if label not in self._labels:
                raise ValueError(f'unknown label {label}')
            # The branch offset is relative to the delay slot.
            offset = (self._labels[label] - (index * 4 + 4)) // 4
            if not -0x8000 <= offset <= 0x7FFF:
                raise ValueError(f'branch to {label} is out of range')
            # A `beq` placeholder stores the opcode; `bne` placeholders are 0.
            opcode = self._words[index] >> 26
            if opcode not in (0x04, 0x05):
                opcode = 0x05
            self._words[index] = (opcode << 26) | (rs << 21) | (rt << 16) | (offset & 0xFFFF)
        return b''.join(word.to_bytes(4, 'little') for word in self._words)


def build_shellcode(blob_length: int) -> bytes:
    """MIPS code that applies the resident blob and resumes the 6502 handler."""

    if blob_length <= 0:
        raise ValueError('the RAM patch blob length must be positive')
    code = _Assembler()
    code.addiu(_SP, _SP, -32)
    code.sd(_RA, 0, _SP)
    code.sd(_A0, 8, _SP)

    # Refresh the cached view of the blob written through the uncached mirror.
    code.li(_T1, _UNCACHED_MIRROR + RESIDENT_BLOB)
    code.li(_T2, RESIDENT_BLOB)
    code.li(_T3, align(blob_length, 8))
    code.label('copy')
    code.ld(_T0, 0, _T1)
    code.sd(_T0, 0, _T2)
    code.addiu(_T1, _T1, 8)
    code.addiu(_T2, _T2, 8)
    code.addiu(_T3, _T3, -8)
    code.bne(_T3, _ZERO, 'copy')
    code.nop()

    # `M-HUNTER\0\0` header and the length the applier requires to be non-zero.
    code.li(_T0, HEADER)
    code.li(_T1, 0x55482D4D)  # "M-HU"
    code.sw(_T1, 0, _T0)
    code.li(_T1, 0x5245544E)  # "NTER"
    code.sw(_T1, 4, _T0)
    code.sw(_ZERO, 8, _T0)
    code.li(_T1, blob_length)
    code.sw(_T1, HEADER_LENGTH, _T0)

    _call_applier(code, 0, 0)
    for slot in (1, 2, 3, 4):
        # `overlay_id(slot)` is the module currently loaded in that slot; 0
        # means there is nothing to apply there, as in the NA helper.
        code.li(_T0, OVERLAY_ID)
        code.li(_A0, slot)
        code.jalr(_T0)
        code.nop()
        label = f'eu-slot-{slot}'
        code.beq(_V0, _ZERO, label)
        code.nop()
        _call_applier(code, slot, None)
        code.label(label)

    _flush(code, 0)
    code.li(_T0, HANDLER_6502_REPLY)
    code.li(_T1, HANDLER_6502)
    code.sw(_T1, 0, _T0)
    _flush(code, 0)
    _flush(code, 2)

    code.ld(_RA, 0, _SP)
    code.ld(_A0, 8, _SP)
    code.addiu(_SP, _SP, 32)
    code.li(_T0, HANDLER_6502)
    code.jr(_T0)
    code.nop()
    return code.encode()


def build_na_shellcode(blob_length: int) -> bytes:
    """NA MIPS code that applies the resident blob and resumes the 6502 handler.

    The blob was written through the uncached mirror, so the shellcode first
    copies it to the cached resident buffer (the applier reads that view).  It
    then writes the `M-HUNTER` header and calls the applier for key 0 and for
    the overlays loaded in slots 1-3, exactly like the official load path
    `0x00258908`.  Finally it restores the 6502 reply slot, flushes the caches
    and tail-calls the real handler with the connection context still in `a0`.
    """

    if blob_length <= 0:
        raise ValueError('the RAM patch blob length must be positive')
    code = _Assembler()
    code.addiu(_SP, _SP, -32)
    code.sd(_RA, 0, _SP)
    code.sd(_A0, 8, _SP)

    # Refresh the cached view of the blob written through the uncached mirror.
    code.li(_T1, NA_UNCACHED_BLOB)
    code.li(_T2, NA_RESIDENT_BLOB)
    code.li(_T3, align(blob_length, 8))
    code.label('na-copy')
    code.ld(_T0, 0, _T1)
    code.sd(_T0, 0, _T2)
    code.addiu(_T1, _T1, 8)
    code.addiu(_T2, _T2, 8)
    code.addiu(_T3, _T3, -8)
    code.bne(_T3, _ZERO, 'na-copy')
    code.nop()

    # `M-HUNTER\0\0` header and the length the applier requires to be non-zero.
    code.li(_T0, NA_HEADER)
    code.li(_T1, 0x55482D4D)  # "M-HU"
    code.sw(_T1, 0, _T0)
    code.li(_T1, 0x5245544E)  # "NTER"
    code.sw(_T1, 4, _T0)
    code.sw(_ZERO, 8, _T0)
    code.li(_T1, blob_length)
    code.sw(_T1, NA_HEADER_LENGTH, _T0)

    _call_applier(code, 0, 0, address=NA_APPLIER)
    for slot in (1, 2, 3):
        # `overlay_id(slot)` is the module currently loaded in that slot; 0
        # means nothing to apply (the shipped blobs key only loaded overlays).
        code.li(_T0, NA_OVERLAY_ID)
        code.li(_A0, slot)
        code.jalr(_T0)
        code.nop()
        label = f'na-slot-{slot}'
        code.beq(_V0, _ZERO, label)
        code.nop()
        _call_applier(code, slot, None, address=NA_APPLIER)
        code.label(label)

    _flush(code, 0, address=NA_FLUSH_CACHE)
    code.li(_T0, NA_HANDLER_6502_REPLY)
    code.li(_T1, NA_HANDLER_6502)
    code.sw(_T1, 0, _T0)
    _flush(code, 0, address=NA_FLUSH_CACHE)
    _flush(code, 2, address=NA_FLUSH_CACHE)

    code.ld(_RA, 0, _SP)
    code.ld(_A0, 8, _SP)
    code.addiu(_SP, _SP, 32)
    code.li(_T0, NA_HANDLER_6502)
    code.jr(_T0)
    code.nop()
    return code.encode()


def _call_applier(
    code: _Assembler, slot: int, overlay: int | None, *, address: int = APPLIER
) -> None:
    code.li(_T0, address)
    code.li(_A0, slot)
    if overlay is None:
        code.move(_A1, _V0)
    else:
        code.li(_A1, overlay)
    code.jalr(_T0)
    code.nop()


def _flush(code: _Assembler, operation: int, *, address: int = FLUSH_CACHE) -> None:
    code.li(_T0, address)
    code.li(_A0, operation)
    code.jalr(_T0)
    code.nop()
