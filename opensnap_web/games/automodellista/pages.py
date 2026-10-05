"""Auto Modellista static browser pages (information, taboo list, runtime patches)."""

# `INFO_MSG` text (at most 767 bytes, `browser.bin` `0x006d95c4`) is drawn by
# `nwDispStr_Html` (`SLUS_206.42` `0x0028dd80`), whose tags are `BODY SIZE COLOR
# BR CENTER LEFT RIGHT END LF` (table `0x00371100`): the same set as Monster
# Hunter's `TOP_INFOR.HTM`, so the welcome text uses the same markup.
AM_INFO_MESSAGE = (
    '<BODY><SIZE=2><LF=1><CENTER>Welcome to openSNAP!<BR><BR>'
    '<LEFT>This server runs openSNAP, the open source<BR>'
    '<LEFT>SN@P server project:<BR>'
    '<CENTER>https://github.com/GrenderG/openSNAP<BR><BR>'
    '<LEFT>It is brought to you by mholdschool.com.<BR><BR>'
    '<LEFT>Thank you for playing and enjoying the western<BR>'
    '<LEFT>releases of Auto Modellista online once again!<BR><BR>'
    '<RIGHT>- Grender<END>'
)
AM_INFO_PAGE = f"""<html><head>
<!--AM-USA-INFORMATION-->
</head>
<!--
<CSV>
"INFO_TAG = openSNAP",
"INFO_MSG = {AM_INFO_MESSAGE}",
</CSV>
-->
</html>
"""


# `am_taboo.html` is not a human-facing release page.
#
# Reverse-engineered release mechanism:
# - `amus_bin/browser.bin` maps `AM-USA-GAME-TABOO` to special-tag type `0x27`,
#   then `special_tag_check` arms parser mode `12`.
# - after a `<CSV>` marker, `get_crs_shadow_data(12)` decodes the page into the
#   `Net_Kinshi_buff` table used by `cmn_mongon_check` as an extra downloadable
#   taboo-word list layered on top of the game's built-in word filters.
# - the decoded table uses the same packed `check_mongon` layout as the retail
#   static list: `14` text bytes per slot plus metadata/continuation state in
#   byte `15`, allowing long taboo phrases to span multiple `16`-byte records.
#
# So the original server-side contract is effectively:
# - `<!--AM-USA-GAME-TABOO-->`
# - one multi-line quoted `<CSV>` field containing taboo phrases in that packed
#   transport format.
#
# This placeholder intentionally omits that contract. It keeps the fetch path
# live without populating the downloadable taboo table, so only the game's
# built-in filters remain active in openSNAP for now.
AM_TABOO_PAGE = '<html><body>am_taboo</body></html>\n'
# `patch*.html` is not a cosmetic page family in either Beta1 or release.
#
# Reverse-engineered browser contract:
# - `AM-USA-GAME-PROG` is special-tag type `0x26` in both overlays.
# - `special_tag_check` maps that to parser mode `11`.
# - once the page also exposes a `<CSV>` block, `get_crs_shadow_data(11)`
#   decodes the numeric payload directly into the same `0x20000` patch buffer
#   later executed by main-ELF `PatchExec`.
# - the first CSV byte is a fixed chunk id `'1'..'5'`; chunks 1..4 fill
#   `0x7000` bytes each and chunk 5 fills the final `0x4000`.
#
# So:
# - Beta1 `/amusa/patch1.html` .. `/amusa/patch5.html`
# - Release `/amusa/patch/2/am_patch1.html` .. `/amusa/patch/2/am_patch5.html`
# are five transport shards of one runtime patch program, not patch notes.
#
# Until a patch program exists the pages are served empty: without the
# `AM-USA-GAME-PROG` tag the browser never arms parser mode 11, so nothing is
# loaded or executed.
AM_PATCH_PAGE = ''
AM_PATCH_PAGE_NUMBERS = range(1, 6)
