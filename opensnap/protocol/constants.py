"""SNAP wire-level constants."""

import struct

# Footer markers and accepted trailer bytes.
FOOTER_MARKER = 0xBA476611
FOOTER_BYTES = struct.pack('>L', FOOTER_MARKER)
FOOTER_MARKER_KAGE = 0xBA476610
FOOTER_BYTES_KAGE = struct.pack('>L', FOOTER_MARKER_KAGE)
ACCEPTED_FOOTER_BYTES = (FOOTER_BYTES, FOOTER_BYTES_KAGE)

# Packet framing sizes.
HEADER_SIZE = 16
FOOTER_SIZE = 4

# Header word masks.
TYPE_MASK = 0xFC00
LENGTH_MASK = 0x03FF

# Transport flags from `kkSetMessage`/`kkSend*` builders and live traces.
FLAG_RELAY = 0x0400
FLAG_MULTI = 0x0800
FLAG_LOBBY = 0x1000
FLAG_RESPONSE = 0x4000
FLAG_RELIABLE = 0x8000

# Channel selectors derived from transport flags.
CHANNEL_ROOM = 0x2000
CHANNEL_MASK = CHANNEL_ROOM | FLAG_LOBBY
CHANNEL_LOBBY = CHANNEL_ROOM | FLAG_LOBBY
RELAY_CONTEXT_MASK = CHANNEL_MASK | FLAG_RELAY
TYPE_ROOM_RELAY = CHANNEL_ROOM | FLAG_RELAY
TYPE_LOBBY_RELAY = FLAG_LOBBY | FLAG_RELAY

# Bootstrap login-failure reason codes for `CMD_BOOTSTRAP_LOGIN_FAIL (0x2e)`.
# `SLUS_206.42` client behavior in `ResultLoginCallBack`:
# - reason `0x13`: dedicated invalid-password branch
#   (`To_ErrorLogOut(5)`, `kk_return_data = 0x32c`).
# - reasons `0x00..0x18` except `0x13`: generic login-fail branch
#   (`To_ErrorLogOut(4)`, `kk_return_data = 0x334 + reason`).
# - reasons `>= 0x19`: generic fallback branch
#   (`To_ErrorLogOut(4)`, `kk_return_data = 0x332`).
# Exact semantics for non-`0x13` reason values are not labeled in the binary;
# the client forwards them numerically through the generic path.
BOOTSTRAP_LOGIN_FAIL_REASON_UNKNOWN = 0x00
BOOTSTRAP_LOGIN_FAIL_REASON_GENERIC = 0x01
BOOTSTRAP_LOGIN_FAIL_REASON_INVALID_PASSWORD = 0x13
