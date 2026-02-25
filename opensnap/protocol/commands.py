"""Command identifiers and command-table notes."""

# Reference points from the SNAP command table notes.
# - 0x002F3C30 `kkSetMessage` for outbound command registration.
# - 0x003AC280 `jpt_kkCommand` and 0x002E9C70 `kkSetCallBackFunction`.
# - 0x002EE030 callback dispatch for special result-wrapper paths.

# Full command table documented from SNAP packet notes.
CMD_ACK = 0x00
CMD_LOGIN_TO_KICS = 0x01  # Common packet context: 0x3000.
CMD_LOGOUT_CLIENT = 0x02
CMD_CREATE_LOBBY = 0x03
CMD_CREATE_GAME_ROOM = 0x04  # Common packet context: 0xB000.
CMD_DELETE = 0x05  # Used by lobby and room delete flows.
CMD_JOIN = 0x06  # Used for both lobby and room joins by channel flags.
CMD_LEAVE = 0x07  # Used for both lobby and room leaves by channel flags.
CMD_CHANGE_ATTRIBUTE = 0x08
CMD_QUERY_ATTRIBUTE = 0x09  # Lobby or room attribute query by channel flags.
CMD_QUERY_USER = 0x0A  # Lobby or room query by channel flags.
CMD_QUERY_GAME_ROOMS = 0x0B  # Common packet context: 0xB000.
CMD_CHANGE_USER_PROPERTY = 0x0C  # Common packet context: 0xA000.
CMD_CHANGE_USER_STATUS = 0x0D  # Common packet context: 0xA000.
CMD_QUERY_LOBBIES = 0x0E  # Common packet context: 0xB000.
CMD_SEND = 0x0F  # Chat or game packet depending on type flags.
CMD_SEND_TARGET = 0x10  # Targeted variant with payload subcommands.
CMD_SEND_GAME_PACKET_TO_GAME_SERVER = 0x11
CMD_ASK_START_VOICE_CHAT = 0x12  # Common packet context: 0x2000.
CMD_FINISH_VOICE_CHAT = 0x13  # Common packet context: 0x2000.
CMD_SEND_ECHO = 0x14
CMD_SEARCH_USERS = 0x18
CMD_RESULT_WRAPPER = 0x28  # Special-case result command.
CMD_RESULT_LOGIN_TO_KICS = 0x29  # KICS login result command.
CMD_LOGIN_CLIENT = 0x2C  # Bootstrap login start.
CMD_BOOTSTRAP_LOGIN_SUCCESS = 0x2D
CMD_BOOTSTRAP_LOGIN_FAIL = 0x2E
CMD_BOOTSTRAP_FAILURE = 0x31  # Observed in command tables, not implemented yet.
CMD_SEND_VOICE_CHAT = 0x32  # Common packet context: 0x2000.
CMD_CHANGE_WELCOME_MESSAGE = 0x34
CMD_SEND_EMERGENCY_MESSAGE = 0x3E
CMD_TARGET_SEND_EMERGENCY_MESSAGE = 0x3F
CMD_BOOTSTRAP_LOGIN_SWAN = 0x40
CMD_BOOTSTRAP_LOGIN_SWAN_CHECK = 0x41

# identified via 003AC280 jpt_kkCommand and 002E9C70 kkSetCallBackFunction
# those are the commands the game is able to receive
#    0x03: "unknown",                          #00 0594 never set, calls kkCreateLobbySwap
#    0x06: "kkJoinCallBack",
#    0x06: "kkJoinLobbyCallBack",              #03 05A0 set by cpnRegisterCallBack, cmd type &1000
#    0x06: "kkJoinGameRoomCallBack",           #02 059C set by cpnRegisterCallBack, cmd type <> &1000
#    0x04: "kkCreateGameRoomCallBack",         #04 05A4 set by cpnRegisterCallBack
#    0x05: "unknown",                          #01 0594 never set, callback for cmd type &1000
#    0x05: "kkDeleteGameRoomCallBack",         #05 05A8 set by cpnRegisterCallBack, cmd type <> &1000
#    0x07: "kkLeaveCallBack",
#    0x07: "kkJoinLobbyCallBack",              #07 05B0 set by cpnRegisterCallBack, cmd type &1000
#    0x07: "kkLeaveGameRoomCallBack",          #06 05AC set by cpnRegisterCallBack, cmd type <> &1000
#    0x08: "unknown",                          #08 05B4 never set, cmd type <> &1000
#    0x08: "kkChangeLobbyAttributeCallBack",   #09 05B8 set by cpnRegisterCallBack, cmd type &1000
#    0x09: "BgCallBackGetJoinUserLobby",       #17 05F0 set by kkQueryLobbyAttribute, cmd type &1000
#    0x09: "unknown",                          #16 05EC set by kkQueryGameRoomAttribute, cmd type <> &1000
#    0x0A: "unknown",                          #19 05F8 set by kkQueryUserInLobby, cmd type &1000
#    0x0A: "ResultQueryUserInGameRoomCallBack",#18 05F4 set by kkQueryUserInGameRoom, cmd type <> &1000
#    0x0B: "BgCallBackQueryRooms",             #1B 0600 set by kkQueryGameRooms
#    0X0C: "unknown",                          #0A 05BC never set
#    0x0E: "ResultQueryPlazaCallBack",         #1A 05FC set by lbc_top_menu/lbc_login->cpnGetLobbyParamater->kkQueryLobbies
#    0x0E: "ResultQueryLobbiesCallBack",       #1A 05FC set by lbc_in_plaza->cpnGetLobbyParamater->kkQueryLobbies
#    0x0E: "kkJoinLobbyCallBack_Sub",          #1A 05FC set by kkJoinLobbyCallBack->cpnGetLobbyParamater->kkQueryLobbies

#    0x0F: "kkTextChatCallBack",               #0B 05C0 set by cpnRegisterCallBack, cmd type &0400
#    0x0F: "kkTextChatCallBack",               #0D 05C8 set by cpnRegisterCallBack, cmd type &1400
#    0x0F: "amkkGamePacketUdpCallBack",        #14 05E4 set by amkkInitialize, cmd type <> &8000
#    0x0F: "amkkGamePacketRudpCallBack",       #12 05DC set by amkkInitialize, cmd type &8000
#    0x0F: "kkGamePacketRudpCallBack",         #12 05DC set by cpnRegisterCallBack, cmd type &8000

#    0x10: "unknown",                          #0C 05C4 never set, cmd type &0400
#    0x10: "unknown",                          #0E 05CC never set, cmd type &1400
#    0x10: "kkGamePacketTargetRudpCallBack",   #13 05E0 set by cpnRegisterCallBack, cmd type &8000
#    0x10: "unknown",                          #15 05E8 never set, cmd type <> &8000

#    0x12: "unknown",                          #10 05D4 never set
#    0x13: "unknown",                          #11 05D8 never set
#    0x13: "ResultEchoPacketCallBack",         #28 0634 set by kkSendEchoPacket
#    0x14: "ResultEchoPacketCallBack",         #28 0634 set by kkSendEchoPacket
#    0x25: "ResultSearchUserCallBack",         #29 0638 set by kkSearchUsers
#    0x??: "unknown",                          #2A 063C set by kkChangeWelComeMessage

#    0x2d: "kkBootStrapLoginSuccess",          #1C 0604 set by kkLoginClient
#    0x2e: "kkBootStrapLoginFail",             #1C 0604 set by kkLoginClient
#    0x31: "kkBootStrapFailure",               #1C 0604 set by kkLoginClient

#    0x32: "unknown",                          #0F 05D0 never set
#    0x35: "unknown",                          #2D 0648 set by kkSetChatLogCallBack
#    0x40: "kkBootStrapLoginSWAN",
#    0x6E: "unknown",                          #2B 0640 set by kkSetJoinGameClassCallBack
#    0x78: "unknown",                          #2C 0644 set by kkSetLeaveGameClassCallBack

#    0x27, 0x28, 0x29: special cases with subcommands, see pcmds2

#pcmds2 = {
#    # commands identified via loc_2EE030
#    0x01: "ResultLoginCallBack",              #1C 0604 set by kkLoginClient
#    0x03: "unknown",                          #1D 0608 set by kkCreateLobby
#    0x04: "ResultCreateGameRoomCallBack",     #1F 0610 set by lbc_inLobby->cpnCreateRoom->kkCreateGameRoom
#
#    0x05: "unknown",                          #1E 060C set by kkDeleteLobby, cmd type &1000
#    0x05: "unknown",                          #20 0614 set by kkDeleteGameRoom, cmd type <> &1000
#
#    0x06: "ResultJoinClubMeetingCallBack",    #21 0618 set by lbc_top_menu->cpnJoinInLobby->kkJoinToLobby, cmd type &1000
#    0x06: "ResultJoinLobbyCallBack",          #21 0618 set by lbc_in_plaza->cpnJoinInLobby->kkJoinToLobby
#    0x06: "ResultJoinRoomCallBack",           #22 061C set by lbc_in_lobby->kkJoinToGameRoom->kkJoinToGameRoom
#
#    0x07: "ResultLeaveLobbyCallBack_2",       #23 0620 set by ResultLeaveRoomCallBack_2->cpnLeaveLobby->kkLeaveFromLobby, cmd type &1000
#    0x07: "ResultLeaveLobbyCallBack",         #23 0620 set by lbc_in_lobby->cpnLeaveLobby->kkLeaveFromLobby, cmd type &1000
#    0x07: "ResultLeaveRoomCallBack_2",        #24 0624 set by lbc_prelogin->cpnLeaveRoom->kkLeaveFromGameRoom, cmd type <> &1000
#    0x07: "ResultLeaveRoomCallBack",          #24 0624 set by lbc_in_room->cpnLeaveRoom->kkLeaveFromGameRoom, cmd type <> &1000
#
#    0x08: "NoProcessCallBack",                #25 0628 set by kkChangeAttribute
#    0x0C: "ResultChangeUserPropatyCallBack",  #26 062C set by kkChangeUserProperty
#    0x0D: "NoProcessCallBack",                #27 0630 set by kkChangeUserStatus
#
#    0x09: "BgCallBackGetJoinUserLobby",       #17 05F0 set by kkQueryLobbyAttribute, cmd type &1000
#    0x09: "unknown",                          #16 05EC set by kkQueryGameRoomAttribute, cmd type <> &1000
#
#    0x0A: "unknown",                          #19 05F8 set by kkQueryUserInLobby, cmd type &1000
#    0x0A: "ResultQueryUserInGameRoomCallBack",#18 05F4 set by kkQueryUserInGameRoom, cmd type <> &1000
#
#    0x0B: "BgCallBackQueryRooms",             #1B 0600 set by kkQueryGameRooms
#
#    0x0E: "ResultQueryLobbiesCallBack",       #1A 05FC set by lbc_in_plaza->cpnGetLobbyParamater->kkQueryLobbies
#    0x0E: "kkJoinLobbyCallBack_Sub",          #1A 05FC set by kkJoinLobbyCallBack->cpnGetLobbyParamater->kkQueryLobbies
#
#    0x0F: "kkTextChatCallBack",               #0B 05C0 set by cpnRegisterCallBack, cmd type &0400
#    0x0F: "kkTextChatCallBack",               #0D 05C8 set by cpnRegisterCallBack, cmd type &1400
#    0x0F: "amkkGamePacketUdpCallBack",        #14 05E4 set by amkkInitialize, cmd type <> &8000
#    0x0F: "amkkGamePacketRudpCallBack",       #12 05DC set by amkkInitialize, cmd type &8000
#    0x0F: "kkGamePacketRudpCallBack",         #12 05DC set by cpnRegisterCallBack, cmd type &8000
#
#    0x25: "ResultSearchUserCallBack",         #29 0638 set by kkSearchUsers
#}

# Callback and routing notes from command-table analysis.
# - CMD_JOIN, CMD_LEAVE, CMD_QUERY_ATTRIBUTE, and CMD_QUERY_USER appear in multiple
#   contexts and are disambiguated by packet type flags.
# - CMD_SEND multiplexes lobby chat, room chat, UDP game packets, and reliable game
#   packets depending on type flags and payload subcommands.
# - 0x27, 0x28, and 0x29 are special result-wrapper paths in callback tables.
# - CMD_QUERY_LOBBIES (0x0E) is reused by multiple lobby/menu callback paths.
# - CMD_SEND (0x0F) and CMD_SEND_TARGET (0x10) callback selection depends on
#   chat/game transport flags such as 0x0400, 0x1400, and 0x8000.
# - CMD_SEND_ECHO (0x14) and CMD_FINISH_VOICE_CHAT (0x13) share echo callback flow
#   in command-table notes.
# - CMD_BOOTSTRAP_LOGIN_SUCCESS (0x2D), CMD_BOOTSTRAP_LOGIN_FAIL (0x2E), and
#   CMD_BOOTSTRAP_FAILURE (0x31) share bootstrap result callback registration.
