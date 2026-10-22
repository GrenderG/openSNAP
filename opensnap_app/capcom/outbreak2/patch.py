"""Resident Evil Outbreak File#2 online code patches.

How a server installs a code patch in File#2 NA (`SLUS_209.84`) and EU
(`SLES_533.19`), and how to write one. Integers on the APP wire are
big-endian (`protocol.py`), everything inside the patch blob is little-endian.

Blob (applier NA `0x15e860`, EU `0x1f3640`)
-------------------------------------------
    char magic[8]     "BIO-VOL2"
    u32  count
    count entries:
        u32 key       module the entry patches (below)
        u32 dest      absolute RAM address
        u32 len       bit 31 clear: `len` data bytes follow
                      bit 31 set: a `u32 src` follows; `len & 0x7fffffff`
                      bytes are copied from absolute address `src`
        data          padded to 4 bytes

The applier is called with one key and copies only the entries with that key,
byte by byte, then calls `FlushCache(0)`. There is no checksum or signature:
only the magic is checked. An entry whose key matches nothing (e.g.
`0xffffffff`) is never copied, so it is a free block of code or data that
stays in RAM inside the resident blob (below).

Keys: 0 = main ELF, `n + 1` = overlay `n` (the `MWo3` header word at +4 is the
key). Overlays are loaded with their 0x40-byte header at the load address, so
`dest = load + offset in the file`. Every overlay load runs `FlushCache(2)`
and then applies its key.

    key  module         NA          EU
    0    main ELF       -           -
    1    submain.bin    0x0038c400  0x003ab700
    2    demo.bin       0x0058d000  0x005ac300
    3    game.bin       0x0058d000  0x005ac300
    4    netwk.bin      0x0058d000  0x005ac300
    5    netdnas.bin    0x006ed000  0x011c0000
    6    nethttp.bin    0x006ed000  0x011c0000
    7    insdnas.bin    0x006ed000  0x011c0000
    9    single.bin     0x00810000  0x007dba00
    10   netaq.bin      0x00810000  0x007dba00
    11-15 (EU) english, france, german, italia, spain  0x00875a00

Key 0 is applied at boot and right after an install. Main-ELF code that has
already run may still sit in the I-cache after `FlushCache(0)`; a hook that
must take effect at once can issue `FlushCache(2)` itself.

Delivery and install
--------------------
1. Only the Information connection (APP mode 0, and its repeat, mode 7)
   reads the 6001 flag. Push 6001 `u8 1, field (name[10] + ver[4]), u32 size,
   u32 0`. `name` is the patch version (10 ASCII characters), `ver` a
   display version ("1010" shows as 1.010), `size` the blob size, at most
   0x8000. The client asks 6002 `field name, u32 offset, u16 length`; answer
   each with `field name, u32 offset, field data` holding exactly that slice.
   After the last chunk it closes with 1004 (skipping 6110..6105).
2. The client does not compare `name` with its version: every flag-1 offer
   leads to the prompt "Game version was renewed to version ... save the
   main data to the memory card?" ("No" preselected). No asks whether to
   return to the main menu (leaving online). Yes: netdnas DNAS
   authentication (must succeed), insdnas encrypts the blob in place, the
   save is written, the stored copy is decrypted into the resident blob,
   the version string becomes the 10-character name, and keys 0 and 4 (plus
   10 or 9 when netaq or single is loaded) are applied.
3. Every boot loads the save and, when the stored name sorts above the build
   stamp over 8 characters and the DNAS decrypt succeeds, decrypts it into
   the resident blob, sets the version string to the name and applies key 0.
   Otherwise the patch is dropped and the next full save erases it.

Version string and naming. The version field of the client's 1002 answer is
its build stamp, NA "0503151731" (from `__DATE__`/`__TIME__`), EU
"0507191200", or the installed patch name. Route by the 1002 build byte
(4 = NA, 6 = EU): patches are region-specific. Name a patch with 10 ASCII
characters whose first 8 sort above "05031517" (NA) / "05071912" (EU), e.g.
"1001000000", and push 6001 `u8 1` only while the 1002 version differs from
it: the client itself would install it again on every offer. A newer patch
needs a newer name and simply replaces the old one.

Storage. The stored form is DNAS-encrypted, so a patch can only be installed
over the network. It is the file `patch.bin` (0x10050 bytes) in the main
save directory (`BASLUS-20984OB2`, EU `BESLES-53319OB2`): 16 header bytes
(`u16 0x9012, u16 seed, u16 sum` at +0, the rest random), then 0x8020 words
XORed with a key stream (`k = seed`; per word: `w ^= k; k = k or 1;
k = k * 176 % 0xff53`; `sum` is the 16-bit sum of the plain words). In the
descrambled file the encrypted blob is at 0x10 (0x8000 bytes) and the header
at 0x10010: `char name[10], char ver[4], u32 length @0x14` (0 = none).

    item                          NA                  EU
    build stamp                   "0503151731"        "0507191200"
    stamp in RAM                  0x002f50e8          0x00245e38
    resident blob (0x8000)        0x0030f280          0x00388700
    encrypted copy (0x8000)       0x00307280          0x00380700
    patch header (0x40)           0x00317280          0x00390700
    download buffer (0x8000)      0x006840b0          0x00702140
    6001 / 6002 handlers          0x1fdca0 / 0x1fdfb0 0x1c92d0 / 0x1c95e0

Put hook code only in the resident blob. A reinstall overwrites it with the
encrypted data for a few frames before decrypting, so keep code offsets
stable between patch versions or restore every hooked site.
"""
