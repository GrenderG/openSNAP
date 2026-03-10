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

# Header context bits from `kkSetMessage`/`kkSend*` builders and live traces.
# `SLUS_206.42` proof:
# - `kkSendGamePacket` (`0x002f2034`), `kkSendTargetGamePacket` (`0x002f22d0`),
#   `kkSendGamePacketToGameServer` (`0x002f2980`), and `kkSendEchoPacket`
#   (`0x002f2ae4`) all OR outgoing type flags with `0x2000`.
# - `kkDispatchingOperation` tests `0x1000` (`0x002ed114`) and `0x2000`
#   (`0x002ed144`) independently.
FLAG_RELAY = 0x0400
FLAG_MULTI = 0x0800
FLAG_LOBBY = 0x1000
FLAG_ROOM = 0x2000
FLAG_RESPONSE = 0x4000
FLAG_RELIABLE = 0x8000

# Channel context bits (`0x3000`).
# - mask use: `type_flags & FLAG_CHANNEL_BITS`
# - lobby context value: `FLAG_CHANNEL_BITS` (both bits set)
# - room context value: `FLAG_ROOM` (only room bit set)
FLAG_CHANNEL_BITS = FLAG_ROOM | FLAG_LOBBY
RELAY_CONTEXT_MASK = FLAG_CHANNEL_BITS | FLAG_RELAY
# `SLUS_206.42` / `SLUS_204.98` `kkSendTextChat` send room chat as raw `0xa400`
# and lobby chat as raw `0xb400`, so masking the raw lobby request keeps both
# channel bits set (`0x3400`).
TYPE_LOBBY_RELAY_REQUEST = FLAG_CHANNEL_BITS | FLAG_RELAY
TYPE_ROOM_RELAY = FLAG_ROOM | FLAG_RELAY
TYPE_LOBBY_RELAY = FLAG_LOBBY | FLAG_RELAY
# Bare transport ACK frames from Auto Modellista clients use command `CMD_ACK`
# with these response-channel flags (`0x6000` in captures / stock client flow).
BARE_ACK_FLAGS = FLAG_ROOM | FLAG_RESPONSE

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

# `CMD_RESULT_WRAPPER (0x28)` status/result codes used by Auto Modellista
# join/create/leave callbacks in `SLUS_206.42`:
# - `ResultJoinLobbyCallBack` (`0x002864f0`) treats status byte `0` as success.
# - `ResultJoinRoomCallBack` (`0x00287e40`) treats status byte `0` as success.
# - `ResultCreateGameRoomCallBack` (`0x00288480`) treats status byte `0` as success.
# - Wider callback scan (`Result*CallBack` family) uses the same split:
#   `status == 0` success, explicit error branch on `status == 0x27`.
# - `ResultLoginCallBack` (`0x00285570`) has a secondary reason word when
#   `status == 0x27` (payload `lw 4(a1)`):
#   - reason `0x13` -> `To_ErrorLogOut(5)`, internal error id `0x32c`
#   - reason `< 0x19` and not `0x13` -> `To_ErrorLogOut(4)`, error id `0x334 + reason`
#   - reason `>= 0x19` -> `To_ErrorLogOut(4)`, error id `0x332`
RESULT_WRAPPER_STATUS_OK = 0x00
RESULT_WRAPPER_STATUS_ERROR_DIALOG = 0x27
