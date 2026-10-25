# Monster Hunter client patches

This folder holds the code patches the Capcom APP service can offer to Monster
Hunter clients (6001/6002). `patches.json` lists each blob with its 6001 name
and version, the client versions it is offered to, whether it is `enabled`,
and its `changelog`. The blobs are written by
`tools/monsterhunter/build_eu_patch.py` and `build_na_patch.py`; the format
and install flow are documented in
`opensnap_app/capcom/monsterhunter/patch.py`.

`eu_official_1001.bin` is Capcom's own EU patch 1001 (name `0509261742`),
decrypted from a retail game save: viciousShadow's original save data, thanks
to whom it could be extracted. It is kept for reference and as the source
of its two fixes, which `build_eu_patch.py` copies into `eu_custom_1002.bin`.
That blob is built on the official 1001 code and is delivered in RAM to both
the stock and the 1001 EU stamps (see below).

## RAM delivery (the RCE route)

`eu_custom_1002.bin` and `na_custom_1002.bin` are raw `M-HUNTER` blobs with no
DNAS install header, so the 6001/6002 install path cannot use them. Instead the
server delivers a blob during the normal online APP session:

- the 6503 World-list parser writes record 16, whose fields (including a
  255-byte description) land on the client's BSS globals;
- one of the client's own download handlers (6204 on EU, 6002 on NA) is used as
  an arbitrary RAM writer;
- a small MIPS shellcode is placed in the resident patch buffer;
- the client's own `PatchExecCS` applier installs the blob's entries.

Nothing is written to the memory card and no client file, ISO or pnach is
modified. Enabling is `OPENSNAP_MH_RAM_PATCH` for EU and
`OPENSNAP_MH_RAM_PATCH_NA` for NA; leave either empty to disable that platform
(the default). The values are paths to the blobs, loaded at APP startup, and
must be valid `M-HUNTER` blobs that fit the 64 KiB resident buffer with their
shellcode.

### Which clients

| build | stamp | route |
|---|---|---|
| EU retail 1000 | `0502180939` | `OPENSNAP_MH_RAM_PATCH` |
| EU retail 1001 | `0509261742` | `OPENSNAP_MH_RAM_PATCH` (same blob) |
| EU demo | `0412241846` | none |
| EU TLES | `0408131008` | none |
| NA retail | `0407251750` | `OPENSNAP_MH_RAM_PATCH_NA` |
| NA public beta | `0405171956` | none |
| any already patched process | `M-HUNTER` | none (already patched) |

The stamp is the 10-byte runtime version in the client's `1002` answer; for a
stock client it is the executable's build stamp. The gate is an exact match
against the stock stamps above — there is no separate `M-HUNTER` check. Once
the patch runs, the applier overwrites that stamp with `M-HUNTER`; since that is
not a stock value, the exact match already excludes the process and it is not
patched twice.

### The 6503 handler, shared by both routes

- There are 16 World records of 608 bytes. Record 16 is the first record
  outside that array and is where the download globals live.
- The handler rejects `total >= 17` and writes `count` records at
  `cursor + i` (not `start + i`).
- `start + count < total` means "not the last page": it does
  `cursor += count` and the client asks for the next page. Otherwise the page
  completes, the cursor resets to 0 (EU always; NA when its guard is non-zero)
  and the state becomes 8 (`6502`, World status) or 54 (`6403`, Land list).
- Therefore the prelude is a normal `total=16, start=0, count=8` page: it moves
  the cursor from 0 to 8 and makes the client request the next page. That
  request is answered with `start=8, count=9`, which writes records 8..15 and
  then record 16.
- The final page's `total` is the **configured World count**, so the client
  lists only real Worlds. The write index is the cursor, not `total`, so one
  World is enough to reach record 16. A list longer than 16 Worlds cannot spare
  record 16; the handler only accepts `total < 17`.
- Where a completed page resets the cursor (EU always; NA until the burst
  clears its guard), the next record-16 write needs another 8-record prelude.
  Once NA's guard is clear, completions keep the cursor at 8.
- Pages must fit the client's 768-byte APP frame body. A page that is too large
  for its names or descriptions keeps the normal paged reply and the client
  stays unpatched.

Record 16's fields and max lengths:

    key 62 at +0x20   raw 8 at +0x6f
    name 15 at +0x5f  description 255 at +0x77

The description covers the APP receive header and the start of the receive
body. The client's field decoder copies a field to its destination and only
then reads the frame header's sequence byte **from that destination** to key
the checksum. A priming description therefore encodes the header's sequence
slot to its own sequence (`_priming_sequence_byte`) and carries the original
header and body bytes; otherwise the client reads a masked byte, computes a
different checksum and closes the connection.

### EU (`SLES_527.07`)

In the flowcharts, `W` is the configured World count.

Record 16 `0x511630`, key `0x511650`, raw `0x51169f`, description `0x5116a7`;
receive header `0x5116c0`, body `0x5116c8`. 6204 writes at
`[gp+620] = 0x00a7a200 + offset`; resident buffer `0x2039e300`, header
`0x003ae300`, applier `0x00248350`, 6502-reply slot `0x0032ac0c`
(normal `0x002a2420`).

```
client                                       server
 6503 start=0 count=8 ---------------------> prelude: total=16 start=0 count=8
   cursor 0 -> 8, state 6
 6503 start=8 count=8 ---------------------> final A: total=W start=8 count=9
   record 16 -> 6204 globals (X=payload, array[1]=end)
   completes: cursor 0, state 8
   6502 -----------------------------------> ignored
                                             upload: 6204 xN -> 0x2039e300
                                               (754-byte fields: blob+pad+shellcode)
   payload download completes -> 6211 ----> ignored
   [pushed] prelude: total=16 start=0 count=8
   cursor 0 -> 8, state 6
   6503 -----------------------------------> ignored
   [pushed] final B: total=W start=8 count=9
   record 16 -> 6204 globals (X=0x32ac0c)
   completes: cursor 0, state 8
                                             handler write: 6204 -> 0x32ac0c
   6502 -----------------------------------> ANSWERED -> shellcode runs
```

The burst is `[uploads..., prelude, final B, handler write]`, sent immediately
after final A. The client processes the handler write before the answered 6502
reply (TCP order). The shellcode copies the blob to the cached resident buffer,
writes the `M-HUNTER` header, calls the applier for key 0 and the loaded
overlays, flushes the caches, restores the 6502 handler and tail-calls
`0x002a2420`. It preserves `ra`, `a0`, `sp`, `gp`, `s0`, `s1`.

### NA (`SLUS_208.96`)

Record 16 `0x552930`, key `0x552950`, name `0x55298f`, raw `0x55299f`,
description `0x5529a7`; receive header `0x552a20`, body `0x552a28`. 6002 name
`0x5529e1`, size `0x5529fc`, write base `0x552a00`, guard `0x00399654`.
6204 base `0x00a7a200`, expected index `0x552990`, expected offset `0x552994`,
end-offset table `0x5529bc`. Resident buffer `0x203d9980`, header `0x003e9980`,
applier `0x00258400`, 6502-reply slot `0x00369ab4` (normal `0x002b2270`).

NA's 6002 handler does nothing unless the guard is 0, but the World flow leaves
it 1. The reply therefore primes both handlers: 6204 (no guard) clears the
guard, then 6002 can upload and write the handler slot. Record 16's name field
overlaps 6204's expected index/offset; its description overlaps the 6204 table
and the 6002 name/size/base; its key field overlaps the client's `1002` login
block (`0x552950`), so the priming key restores that block (`0x541130`, 2, 1,
`0x3a2760`, 0: the user-ID and version buffer pointers and two flags) instead
of zeroing it.

```
client                                       server
 6503 start=0 count=8 ---------------------> P0: total=16 start=0 count=8
   cursor 0 -> 8, state 6
 6503 start=8 count=8 ---------------------> F0: total=W start=8 count=9
   record 16: 1002 login block, 6204 index/offset,
             6204 table, 6002 name/size/base=payload
   completes: guard=1 -> cursor 0, state 8
   6502 -----------------------------------> ignored
                                             (1) 6204 -> 0x399654 = 0 (guard off)
   state 20 -> 6211 -----------------------> ignored
                                             (2) 6002 x123 -> 0x203d9980
   state 23 -> 6002 x123 ------------------> ignored
   [pushed] P1: total=16 start=0 count=8
   cursor 0 -> 8, state 6
   6503 -----------------------------------> ignored
   [pushed] F1: total=W start=8 count=9
   record 16: 6002 base = 0x369ab4-0xfffc
   completes: guard=0 -> state 54, cursor stays 8
   6403 -----------------------------------> ignored
                                             (3) 6002 -> 0x369ab4 = shellcode
   [pushed] F2: 6002 base = 0x399654-0xffff
   completes: guard=0 -> state 54, cursor stays 8
   6403 -----------------------------------> ignored
                                             (4) 6002 -> 0x399654 = 2 (guard on)
   [pushed] F3: total=W start=8 count=9
   completes: guard=2 -> cursor 0, state 8
   6502 -----------------------------------> ANSWERED -> shellcode runs
```

Why the guard dance:

- F0 completes with guard 1, so it chooses state 8 and resets the cursor.
- The 6204 write clears the guard; 6002 is then enabled.
- F1 and F2 complete with guard 0, so they choose state 54 and leave the cursor
  at 8. They can therefore re-write record 16 without another prelude.
- The guard 6002 write sets 2; F3 then chooses state 8 and sends the 6502 that
  runs the shellcode.
- The guard stays 2 for the session: later World pages take the 6502 path and
  further 6002 downloads are refused until the game is reset.

The NA shellcode is the same idea as EU: copy the blob to the cached resident
buffer, write the header at `0x003e9980`, call `0x00258400` for key 0 and the
loaded overlays, flush, restore `0x002b2270` and tail-call it.

### Requests the burst ignores

The burst already pushed every frame, so the client's follow-up requests must
stay unanswered: answering would change the state before the next injected
frame. Counts per burst:

| request | EU | NA | cause |
|---|---:|---:|---|
| `6502` World status | 1 | 1 | first final page completes to state 8 |
| `6503` World page | 1 | 1 | injected prelude completes to state 6 |
| `6211` Market | 2 | 1 | each injected 6204 download completes to state 20 (EU: payload + handler; NA: guard) |
| `6403` Land list | 0 | 2 | NA F1/F2 complete with the guard clear |
| `6002` patch chunk | 0 | 125 | NA 123 uploads + handler + guard writes |

### Lifetime, safety, updating

- RAM-only. The applier is Capcom's `PatchExecCS`; the delivery never calls the
  memory-card API. The patch lasts for the current game process (World changes
  and reconnects keep it) and is gone on reset; a new process is patched again
  on its next online session.
- The loader validates the `M-HUNTER` magic, entry structure and resident-buffer
  fit, but not entry destination addresses. A malformed blob can write where it
  should not; use a valid blob built for the same client build. A crash during a
  save can still corrupt that save, as with any game crash; back up saves before
  testing a new blob.
- If the connection drops after the handler-slot write but before the answered
  `6502`, the slot stays hijacked in that process; the shellcode is still
  resident and a later `6502` reply would run it, or a reset clears it.
- To update: replace the `.bin` at the configured path, restart the APP service
  (blobs load at startup) and reset the game (otherwise the client reports
  `M-HUNTER` and is skipped). No code changes are needed if the blob keeps the
  `M-HUNTER` format and fits. EU and NA blobs are not interchangeable.

## Why a raw blob is rejected

Before saving a downloaded patch, retail clients pass it through the DNAS2
install library (`dnas_ins` overlay, `InetDNAS2PersonalEncode`). That library
only accepts data that starts with a 0x580-byte DNAS install header:

- **The header holds RSA-1024 signatures.** Its records are checked with
  public keys stored in `dnas_ins` (the first with key 83). The matching
  private keys belong to Sony and are not on the client, so nobody outside
  Sony can sign a new header.
- **A raw blob shorter than 0x580 bytes** (like the current ones) makes the
  client wait forever: the DNAS install bar stops at 100%.
- **A raw blob of 0x580 bytes or more** fails the signature check and the
  client shows a DNAS error.

This is not a DNAS server problem. The DNAS authentication before the install
succeeds; the client fails afterwards, on the data itself. Both cases were
reproduced by running the clients' own code in an emulator (EU and NA). The
addresses are in `opensnap_app/capcom/monsterhunter/patch.py` and
`tools/dnas/install_packet.py`.

Resident Evil Outbreak uses the same library with the same keys, so it has the
same limit.

## Wrapping a patch with a signed header

`tools/dnas/install_packet.py` builds `[0x580 header][payload]` from a
genuine signed header, for example one taken from a retail install download:

    python3 -m tools.dnas.install_packet --overlay dnas_ins.dec.bin \
        --header header.bin --payload eu_custom_1002.bin -o eu_custom_1002.signed.bin

`--overlay` is the title's `dnas_ins` overlay, decrypted first (the overlay
decrypts its own code at run time; the scheme is described in
`tools/dnas/install_packet.py`). `--inspect` prints what the tool reads from
it. The tool cannot generate a header: without `--header` it stops with an
error.

This cannot carry a custom payload. Capcom's EU patch as stored on the
memory card (0x1f70 bytes) shows the whole format: the 0x580 header, the
encrypted payload from 0x580, and one more RSA-signed record at 0x1ef0. That
record holds a SHA-1 digest of the encrypted patch (0x13f0..0x1ee0 once the
console layer is removed, the data the patch is decrypted from), and the
client compares all 20 bytes with its own digest (`dnas_ins` 0xaa20f0). So a genuine header
only works with the payload it was signed with. Changing any byte of the patch
needs a new signature from Sony's private keys. Only the 128-byte session
header at 0x180 depends on the console, which is why a genuine blob can be
moved from one console to another but not changed.

A custom patch therefore cannot be installed through the signed DNAS step;
use the RAM delivery above instead. Keep these `6001/6002` entries disabled.
