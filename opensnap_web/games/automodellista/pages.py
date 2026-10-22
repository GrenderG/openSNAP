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


# `am_taboo.html` (release only, fetched last by `ms_net_data_get`) adds
# phrases to the network word filter; Beta1 never fetches it. openSNAP serves
# it empty, so only the built-in filters apply. A page that adds phrases:
#
#     <!--AM-USA-GAME-TABOO-->
#     <!--
#     <CSV>
#     "FIRSTPHRASE
#     SECONDPHRASE
#     LASTPHRASE"
#     </CSV>
#     -->
#
# - The tag arms `browser.bin` parser mode 12 (`0x006d9e44`); the `<CSV>` must
#   sit in a later, separate comment, as for the other `AM-USA-*` pages.
# - One phrase per line (CRLF or LF), the first after the opening quote, the
#   last ended by the closing quote; a line starting with `<` ends the list
#   (reader `0x006d84d0`). Lines are at most 767 bytes; the rest is dropped.
#   Avoid empty lines and quotes at the start of a later line: both give an
#   empty phrase.
# - Each phrase is packed into 16-byte slots of `Net_Kinshi_buff`
#   (`0x004d8d80`, 1024 slots, `0x006d96a4`): 14 text bytes, byte 15 = the
#   phrase length on the first slot and `0xff` on continuation slots. Nothing
#   clears the buffer, so a shorter list leaves the tail of a longer earlier
#   one.
# - `cmn_mongon_check` (`0x002648c0`) checks chat and names against the
#   built-in `check_mongon` table, then this one, on a copy of the text with
#   letters uppercased and `@ $ 5 < ( ! 1 2 0` read as `A S S C C I I Z O`
#   (`cmn_mongon_check_filter` `0x00264bb0`); matches are replaced with `*`.
#   Write phrases in that form: uppercase, no lowercase or those symbols.
AM_TABOO_PAGE = ''
# `patch*.html` carry the runtime code patch (`patch.py`). They are served
# empty: without the `AM-USA-GAME-PROG` tag nothing is loaded or executed.
AM_PATCH_PAGE = ''
AM_PATCH_PAGE_NUMBERS = range(1, 6)
