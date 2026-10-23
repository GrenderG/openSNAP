"""Resident Evil Outbreak File#1 online code patches.

How a server installs a code patch in File#1 v1/v2 (`SLUS_207.65`) and the
public beta (`SLUS_290.89`), and how to write one. Addresses are v2 unless a
table says otherwise; integers on the APP wire are big-endian (`protocol.py`),
everything inside the patch blob is little-endian.

Blob (applier v2 `0x1f71f0`)
----------------------------
    char magic[8]     "BIO-V1-2" (v2) or "BIO-VOL1" (v1, beta)
    u32  count
    count entries:
        u32 key       module the entry patches (below)
        u32 dest      absolute RAM address
        u32 len       bit 31 clear: `len` data bytes follow
                      bit 31 set: a `u32 src` follows; `len & 0x7fffffff`
                      bytes are copied from absolute address `src`
        data          padded to 4 bytes

The applier is called with one key and copies only the entries with that key,
byte by byte (no alignment needed), then calls `FlushCache(0)` (D-cache
writeback only). There is no checksum or signature: only the magic is
checked. An entry whose key matches nothing (e.g. `0xffffffff`) is never
copied, so it is a free block of code or data that stays in RAM inside the
resident blob (below); hook it with `j`/`jal` to `resident + offset`.

Keys: 0 = main ELF, `n + 1` = overlay `BIN/<n>.DAT`, applied every time that
overlay loads (v2 `0x1a971c`). Overlays are loaded with their 0x40-byte
`MWo3` header at the load address, so `dest = load + offset in the unpacked
file`:

    key  module        load (v1, v2)   key  module        load
    0    main ELF      -               5    netdnas.bin   0x006e0000
    1    submain.bin   0x00370000      6    insdnas.bin   0x006e0000
    2    demo.bin      0x00570000      7    nethttp.bin   0x006e0000
    3    game.bin      0x00570000      9    netaq.bin     0x00810000
    4    netwk.bin     0x00570000      10   single.bin    0x00810000

Key 0 is applied at boot and right after an install; overlays sharing a load
address are never resident together.

Retail v1/v2: delivery and install
----------------------------------
1. On lobby entry (APP mode 1; the result upload, mode 0, ignores 6001)
   push 6001 `u8 1, field (name[10] + ver[4]), u32 size, u32 0`. `name` is
   the patch version (10 ASCII characters), `ver` a display version ("1010"
   shows as 1.010), `size` the blob size, at most 0x8000. The client then
   asks 6002 `field name, u32 offset, u16 length` in order; answer each with
   `field name, u32 offset, field data` holding exactly that slice. A name
   that differs from the offer fails the connection (D963). The data lands
   in the download buffer.
2. After the connection, `netwk` `0x60932c` compares `name` with the
   client's version string over 10 characters; equal skips the install.
3. The client reads its system save (`BASLUS-20765NETBIO`; none: "System data
   could not been found") and asks "Game version was renewed ... save?". No
   disconnects. Yes: netdnas DNAS authentication (`0x7507d0`, must succeed),
   then insdnas `0x7099d0` (InetDNAS2PersonalEncode, the same DNAS2 install
   library as Monster Hunter's `dnas_ins`) personalizes the blob. It must
   start with a 0x580 DNAS install header of RSA-1024 signatures (key 83 and
   two chained keys; the private keys are not on the client), so a
   server-authored blob cannot pass: 0x580 bytes or more fail the signature
   check (error state 34), less than 0x580 polls forever (both emulated; the
   check is `0x706a48`, key 83). On success the save is written, the stored
   copy is decrypted (`0x709b40`) into the resident blob and keys 0, 4 and the
   loaded `0x810000` overlay's key are applied. A failed install wipes both
   buffers (`0x1f7160`) and applies nothing.
4. Every boot loads the save, decrypts the stored blob into the resident
   blob and applies key 0; later overlay loads apply their keys. A failed
   decrypt clears the patch.

Version string and naming. The client's version string is its build stamp
(`YYMMDDhhmm` of its `__DATE__`/`__TIME__`): v1 "0402271350", v2
"0404141723". It is the version field of the client's 1002 answer, so the
server can see what is installed. At boot the stored name replaces its
first 8 characters; the applier does the same when `count != 0` and the
stamp sorts below the name. So name a patch `XXXXXXXX` + the build's last two
characters (v1 "50", v2 "23"), with `XXXXXXXX` above "04022713" (v1) /
"04041417" (v2), e.g. "1001000023" for v2. Then the installed client
reports exactly the patch name, and the server offers 6001 `u8 1` only when
the 1002 version differs from it. Any other name is offered again every
session.

Storage. The stored form is the DNAS-personalized blob, so retail v1/v2 take
neither a server-authored patch nor one written into a save. The save is
0x3fcc8 bytes: `u16 0x5963, u16 seed, u16 sum, u16 0`, then 0x1fe5c words
XORed with a key stream (`k = seed`; per word: `w ^= k; k = k or 1;
k = k * 176 % 0xff53`; `sum` is the 16-bit sum of the plain words). In the
descrambled file the encrypted blob is at 0x2fcc8 (0x8000 bytes) and the
header at 0x37cc8: `char name[10], char ver[4], u32 length @0x14`
(0 = no patch). Every game save rewrites both from RAM.

    item                          v1                  v2
    magic                         "BIO-VOL1"          "BIO-V1-2"
    build stamp                   "0402271350"        "0404141723"
    stamp in RAM                  0x00315c28          0x003165a8
    applier                       0x001f68e0          0x001f71f0
    resident blob (0x8000)        0x00366700          0x00367080
    download buffer (0x8000)      0x0035e700          0x0035f080
    patch header (0x40)           0x0036e700          0x0036f080
    6001 / 6002 handlers          0x1f47e0 / 0x1f4a00 0x1f50f0 / 0x1f5310
    netwk name check              0x0060935c          0x0060932c

Do not place code in the download buffer or the header; the resident blob is
written only at boot, install and when a decrypt fails.

Public beta (`SLUS_290.89`)
---------------------------
Same blob ("BIO-VOL1") and applier (`0x1f39e0`), but the patch is plaintext,
installed without prompt or DNAS and lost at power-off.

- 6001 is read only on lobby entry (the beta's mode 0). Push 6001 `u8 1,
  field (name[10] + ver[4]), u32 size <= 0x8000, u32 0`. The client then
  sends all its 6002 requests at once (chunks of `256 - (len(name) + 12)`
  bytes, 216 for a 10-character name); answer them in order. After the last
  reply push 6001 `u8 0`: only a flag-0 offer after a complete download
  installs it (`netwk` `0x605ef0`).
- The client compares `name` with its stamp "0312181725" (10 characters);
  equal skips. Otherwise it copies the blob to the resident buffer, applies
  keys 0, 4 and the loaded `0x820000` overlay's key, and restarts the
  lobby-entry connection, which then must get 6001 `u8 0` only.
- The applier sets the stamp to `name[0:8] + "25"` when the name sorts above
  it, so name patches `XXXXXXXX25`.
- Resident blob `0x00357080..0x0035f080`, download buffer `0x0034f080`,
  header `0x0035f080`. Keys as retail; loads: submain `0x00370000`,
  demo/game/netwk `0x00570000`, netdnas/insdnas/nethttp `0x006fa000`,
  editor/netaq/single `0x00820000` (keys 8, 9, 10).
"""
