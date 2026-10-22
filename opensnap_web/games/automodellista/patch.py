"""Auto Modellista online code patches.

How a web server installs a code patch in Auto Modellista release
(`SLUS_206.42`), Public Beta vol.2 (`SLUS_280.31`) and Public Beta vol.1
(`SLUS_204.98`), and how to write one. The patch lives only in RAM: it is
downloaded each time the player goes online and applied on every lobby entry,
with no memory card, version check, DNAS step or signature.

Buffer (`PatchExec`, release `0x224af0`)
----------------------------------------
`PATCH_ADRS` is a 0x20000-byte little-endian buffer:

    s32 count             nothing is applied when <= 0
    count entries:
        u32 dest          absolute RAM address
        u32 len           bit 31 clear: `len` data bytes follow, padded to 4
                          bit 31 set: a `u32 src` follows; `len & 0x7fffffff`
                          bytes are copied from absolute address `src`

Entries are copied byte by byte, then `FlushCache(0)` runs (D-cache
writeback only, so code that already ran may still sit in the I-cache; a
hook that must take effect at once can issue `FlushCache(2)` itself). An
all-zero 8-byte entry is a no-op. `count` is not bounds-checked.

`PatchExec` runs on every lobby entry, including returns from a race, so
entries must be safe to apply repeatedly. At that point the main ELF and
`net.bin` (`0x00680000`) are loaded; `browser.bin` is loaded again right
after, so do not patch it, and do not patch the `0x00640000` game slot. The
buffer is untouched until the next download clears it, so code and data
can live in it: point to `PATCH_ADRS + offset`.

Pages
-----
The buffer is filled by five web pages, fetched after `am_info`, `am_rule`
and `am_rank` (`ms_net_data_get`); the buffer is zeroed after `am_rank`. A
page that fails to load stops the remaining fetches; an empty page writes
nothing. Each page:

    <!--AM-USA-GAME-PROG-->
    <!--
    <CSV>
    "1",
    "<hex>"
    </CSV>
    -->

- The tag comment must follow `<!--` with no space (case-insensitive). The
  `<CSV>` must sit in a later, separate comment.
- The first quoted string is the window: `'1'..'4'` fill `PATCH_ADRS +
  (n - 1) * 0x7000` (0x7000 bytes), `'5'` fills `+ 0x1c000` (0x4000 bytes).
  The window, not the page number, decides where the data goes.
- The second quoted string is the data, two hex digits per byte. Release and
  vol.2 store `value - 6`, so write `(byte + 6) & 0xff` (bytes 01 02 03 04 ->
  `"0708090A"`); vol.1 stores the plain value (`"01020304"`). One CR and/or LF
  may precede any digit; nothing else (no spaces, tabs or empty lines). The
  closing quote must follow a digit. A bad digit silently gives a wrong byte.
- The browser keeps at most 32767 bytes of a page, so one page carries about
  16 KB of data: a 0x7000-byte window can't be filled whole. Put the entry
  table in window 1 and bulk data in the others, reached with pointer
  entries.

    build      pages                                         PATCH_ADRS  PatchExec
    release    gameweb.../amusa/patch/2/am_patch1..N.html    0x004b8d80  0x00224af0
    vol.2      gameweb.../amusa/patch/1/am_patch1..N.html    0x004b7280  0x00223fa0
    vol.1      web.ftpublicbeta.../amusa/patch1..5.html      0x0051d300  0x00269da0

Release and vol.2 fetch `N` pages, `N` being the last byte (byte 27) of a
28-byte row of the `am_rule` page: row 1 in release (`BsGameRule + 0x40`,
`0x0020b780`), row 0 in vol.2 (`BsGameRule + 0x1b`, `0x0020b460`). 0 fetches
none, 1..5 fetch pages 1..N over HTTP from `gameweb.capcom-am.games.sega.net`,
6 and above fetch pages 1..N-5 over HTTPS from `rankweb.capcom-am.games.sega.net`.
Vol.1 always fetches all five from `web.ftpublicbeta.games.sega.net`. The
vol.2 browser overlay is not in the repository; its `get_shadow_numeric_data6`
sits where release's does, so it is taken to decode like release.
"""
