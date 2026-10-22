# Monster Hunter NA (SLUS_208.96) online code patch: the resident block (key 0xffffffff).
# B = EU UTF-8 text folded to the NA font; A = quest clear time and kill counts sent as EU 6307.
# Built and linked at its resident address by tools/monsterhunter/build_na_patch.py.
        .set noreorder
        .set noat
        .text
        .globl _start
_start:

# ======================================================================================
# B. UTF-8 fold
# ======================================================================================
# fold_core(a0 = src, a1 = dst, a2 = max bytes before the NUL, a3 = 1): folds src into dst (dst == src or a
#   separate buffer; the output never grows), NUL-terminates. a2 = -1: no limit.
# fold_core(a0 = src, a1 = &fold_sink, a2 = -1, a3 = 0): writes nothing that matters, only counts.
# Returns v0 = folded length. Walks like FontPuts 0x1e5ba0: 80-9F/E0-FF lead a 2-byte pair, A0-DF single.
#   C2/C3 + 80-BF         -> one ASCII byte from fold_table (U+0080..U+00FF)
#   E0-EF + 80-BF + 80-BF -> '?'
#   C2/C3 at the end      -> dropped
# Clobbers v0, a0, a1, t0-t4, t9, at.
        .globl fold_core
fold_core:
        move    $v0, $zero
        lui     $t9, %hi(fold_table)
        addiu   $t9, $t9, %lo(fold_table)
1:      lbu     $t0, 0($a0)
        beqz    $t0, 9f
        sltu    $at, $v0, $a2
        beqz    $at, 9f                 # output full (copy mode only)
        lbu     $t1, 1($a0)             # next byte (0 at the end)
        sltiu   $at, $t0, 0x80
        bnez    $at, 7f                 # ASCII: copy
        andi    $t2, $t1, 0xc0
        xori    $t2, $t2, 0x80          # t2 == 0 <=> next is 80-BF
        addiu   $t3, $t0, -0xc2
        sltiu   $t3, $t3, 2
        beqz    $t3, 3f                 # not C2/C3
        nop
        beqz    $t1, 8f                 # C2/C3 cut off at the end: drop it
        nop
        bnez    $t2, 7f                 # C2/C3 + non-continuation: a half-width kana, copy
        andi    $t3, $t0, 1
        sll     $t3, $t3, 6
        andi    $t4, $t1, 0x3f
        or      $t3, $t3, $t4           # code point - 0x80
        addu    $t3, $t3, $t9
        lbu     $t0, 0($t3)             # ASCII fold
        b       7f
        addiu   $a0, $a0, 1             # consume the continuation byte
3:      andi    $t3, $t0, 0xf0
        xori    $t3, $t3, 0xe0
        bnez    $t3, 4f                 # not E0-EF
        lbu     $t4, 2($a0)
        bnez    $t2, 4f
        andi    $t4, $t4, 0xc0
        xori    $t4, $t4, 0x80
        bnez    $t4, 4f
        nop
        li      $t0, 0x3f               # 3-byte UTF-8 -> '?'
        b       7f
        addiu   $a0, $a0, 2
4:      sltiu   $at, $t0, 0xa0          # Shift-JIS lead 80-9F / E0-FF: copy the pair
        bnez    $at, 5f
        sltiu   $at, $t0, 0xe0
        bnez    $at, 7f                 # A0-DF: single byte
        nop
5:      beqz    $t1, 7f                 # lead at the end: copy it alone
        nop
        sb      $t0, 0($a1)
        addu    $a1, $a1, $a3
        addiu   $v0, $v0, 1
        addiu   $a0, $a0, 1
        move    $t0, $t1
7:      sb      $t0, 0($a1)
        addu    $a1, $a1, $a3
        addiu   $v0, $v0, 1
8:      b       1b
        addiu   $a0, $a0, 1
9:      jr      $ra
        sb      $zero, 0($a1)

# u32 fold_vsprintf(char *buf, const char *fmt, va_list ap): vsprintf 0x1753d0, then fold buf in place.
# Replaces `jal 0x1753d0` at 0x1e5810 (flfntPrintf) and 0x12f7bc (font_print_sp); returns the folded length.
        .globl fold_vsprintf
fold_vsprintf:
        addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        jal     0x1753d0
        sw      $a0, 8($sp)
        lw      $a0, 8($sp)
        move    $a1, $a0
        li      $a2, -1
        jal     fold_core
        li      $a3, 1
        ld      $ra, 0($sp)
        jr      $ra
        addiu   $sp, $sp, 16

# u32 fold_strlen(const char *s): strlen of the folded text (what is drawn). Replaces `jal 0x16ff38` (strlen)
# where player text is measured; same clobbers as a call.
        .globl fold_strlen
fold_strlen:
        lui     $a1, %hi(fold_sink)
        addiu   $a1, $a1, %lo(fold_sink)
        li      $a2, -1
        j       fold_core
        move    $a3, $zero

# fold_copy(v1 = src): v0 = FOLD_BUF holding the folded src (NULL stays NULL).
# fold_inplace(v1 = src): folds src in place, v0 = src.
# Both keep a0-a3 and t0-t3 (the EABI argument registers) and every FPR; clobber v0, v1, t4, t9, at, ra.
        .equ    FOLD_MAX, 1020
        .globl fold_copy
fold_copy:
        lui     $v0, %hi(FOLD_BUF)
        b       1f
        addiu   $v0, $v0, %lo(FOLD_BUF)
        .globl fold_inplace
fold_inplace:
        move    $v0, $v1
1:      beqz    $v1, 2f                 # NULL: nothing to fold
        nop
        addiu   $sp, $sp, -160
        sq      $a0, 0($sp)
        sq      $a1, 16($sp)
        sq      $a2, 32($sp)
        sq      $a3, 48($sp)
        sq      $t0, 64($sp)
        sq      $t1, 80($sp)
        sq      $t2, 96($sp)
        sq      $t3, 112($sp)
        sd      $ra, 128($sp)
        sw      $v0, 136($sp)
        move    $a0, $v1
        move    $a1, $v0
        li      $a2, FOLD_MAX
        jal     fold_core
        li      $a3, 1
        lw      $v1, 136($sp)
        lq      $a0, 0($sp)
        lq      $a1, 16($sp)
        lq      $a2, 32($sp)
        lq      $a3, 48($sp)
        lq      $t0, 64($sp)
        lq      $t1, 80($sp)
        lq      $t2, 96($sp)
        lq      $t3, 112($sp)
        ld      $ra, 128($sp)
        addiu   $sp, $sp, 160
2:      jr      $ra
        move    $v0, $v1

# Player text measured on the raw string before it is printed: give the game the folded text instead.
# 0x1185a0 `jal 0x1184c0` (hunter name print, cut to 10 bytes; name at player+2260) -> jal w_name.
        .globl w_name
w_name: addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        jal     fold_copy
        move    $v1, $a0
        move    $a0, $v0
        ld      $ra, 0($sp)
        j       0x1184c0
        addiu   $sp, $sp, 16

# 0x235624 `move s3,a1` in chat log add 0x235610 (wraps into 22/28-column lines; ra and s3 are already saved
# on the stack there) -> jal w_chat: a1 = s3 = folded copy of the message.
        .globl w_chat
w_chat: addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        jal     fold_copy
        move    $v1, $a1
        move    $a1, $v0
        move    $s3, $v0
        ld      $ra, 0($sp)
        jr      $ra
        addiu   $sp, $sp, 16

# 0x234f40 message window setup (wraps and counts letters with 0x12fea0, keeps the text pointer for later
# frames): `addiu sp,sp,-112` -> `j w_msg`. The delay slot `lui at,0x3b` runs first; we fold in place and
# continue at 0x234f44, which sets at again.
        .globl w_msg
w_msg:  addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        jal     fold_inplace
        move    $v1, $a0
        ld      $ra, 0($sp)
        addiu   $sp, $sp, 16
        j       0x234f44
        addiu   $sp, $sp, -112

# lobby 0x5fb348 / 0x5fb400 `jal 0x609f80` (player info panel text runs; f12-f14 = x, y, scale) -> jal w_panel.
        .globl w_panel
w_panel:
        addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        jal     fold_copy
        move    $v1, $a0
        move    $a0, $v0
        ld      $ra, 0($sp)
        j       0x609f80
        addiu   $sp, $sp, 16

# lobby 0x5fc2a0 / 0x5fcdf8 `jal 0x1310f0` and 0x64f87c `jal 0x1310e0` (paragraph wrapped by byte columns,
# text in a3, columns in t0) -> jal w_para / w_para_e0.
        .globl w_para_e0
w_para_e0:
        lui     $v0, %hi(0x1310e0)
        b       1f
        addiu   $v0, $v0, %lo(0x1310e0)
        .globl w_para
w_para: lui     $v0, %hi(0x1310f0)
        addiu   $v0, $v0, %lo(0x1310f0)
1:      addiu   $sp, $sp, -16
        sd      $ra, 0($sp)
        sw      $v0, 8($sp)
        jal     fold_copy
        move    $v1, $a3
        move    $a3, $v0
        lw      $v0, 8($sp)
        ld      $ra, 0($sp)
        jr      $v0
        addiu   $sp, $sp, 16

fold_sink:
        .word   0
        .globl FOLD_BUF
FOLD_BUF:
        .space  1024
        .globl fold_table
fold_table:                             # U+0080..U+00FF -> ASCII (no '~', '%', '\n')
        .ascii  "????????????????????????????????"
        .ascii  " !cLoY|S\"ca\"--r-o+23'uP.,1o\"????"
        .ascii  "AAAAAAACEEEEIIIIDNOOOOOxOUUUUYPs"
        .ascii  "aaaaaaaceeeeiiiidnooooo/ouuuuypy"

# ======================================================================================
# A. Quest records (EU 6307) on NA
# ======================================================================================
        .equ    Q_START, 72
        .equ    Q_CLEAR, 76
        .equ    Q_INFL, 80
        .equ    Q_SAVE, 84
        .equ    Q_BUF, 96
        .align  3
        .globl  QDATA
QDATA:                                  # CNT u16[36] by monster type (EU 0x3aebb4); 1..34 are sent
        .space  72
        .word   0                       # +72 START: clock seconds at Quest_start
        .word   0                       # +76 CLEAR: pending clear seconds, 0 = none
        .word   0                       # +80 INFL: kind in flight (1 or 2)
        .space  12                      # +84 ctx+20/+28/+32 while sending
        .space  184                     # +96 BUF: 8-byte APP header + 176 payload bytes (kind 1 uses 174)

# 0x1f1138 in Quest_enemy_die: `jal 0x1f5a30` -> `jal quest_kill`. a1 = lbu 2(enemy) (EU indexes the same byte),
# a0 = 0x3ea0b0 from the delay slot; tail-jumps to 0x1f5a30 with ra = 0x1f1140. Clobbers t0-t3.
        .globl quest_kill
quest_kill:
        sltiu   $t0, $a1, 35
        beqz    $t0, 1f
        sll     $t1, $a1, 1
        lui     $t2, %hi(QDATA)
        addu    $t2, $t2, $t1
        lhu     $t3, %lo(QDATA)($t2)
        sltiu   $t0, $t3, 9999
        addu    $t3, $t3, $t0           # capped at 9999 like EU
        sh      $t3, %lo(QDATA)($t2)
1:      j       0x1f5a30
        nop

# 0x1f0a3c in Quest_start: `lui at,0x3f` -> `jal quest_start` (the delay slot `sh zero,-24350(at)` still has
# at = 0x3f0000 from 0x1f0a34; 0x1f0a44 reloads at). START = (int)clock 0x51e73c, CLEAR = 0. Clobbers t0, t1, f0.
        .globl quest_start
quest_start:
        lui     $t0, 0x52
        lwc1    $f0, -6340($t0)         # 0x51e73c
        trunc.w.s $f0, $f0              # 0x46000024 = R5900 cvt.w.s (truncates)
        mfc1    $t1, $f0
        lui     $t0, %hi(QDATA)
        addiu   $t0, $t0, %lo(QDATA)
        sw      $t1, Q_START($t0)
        jr      $ra
        sw      $zero, Q_CLEAR($t0)

# 0x1f16f8 in the clear branch (quest state 4): `lui at,0x3f` -> `jal quest_clear` (delay slot
# `sw v0,-24064(at)` uses at = 0x3f0000 from 0x1f16f0). CLEAR = max(now - START, 1). Keeps a0, a1, v1.
        .globl quest_clear
quest_clear:
        lui     $t0, 0x52
        lwc1    $f0, -6340($t0)
        trunc.w.s $f0, $f0
        mfc1    $t1, $f0
        lui     $t0, %hi(QDATA)
        addiu   $t0, $t0, %lo(QDATA)
        lw      $t2, Q_START($t0)
        subu    $t1, $t1, $t2
        bgtz    $t1, 1f
        nop
        li      $t1, 1
1:      jr      $ra
        sw      $t1, Q_CLEAR($t0)

# 0x1f0c78 in Quest_retire_set (only reached when Quest_clear_ck(1) == 0): `li v0,-1` -> `jal quest_retire`;
# zeroes the counts like EU 0x1e6b48 and returns v0 = -1. Delay slot `lui at,0x3f` is harmless.
        .globl quest_retire
quest_retire:
        lui     $t0, %hi(QDATA)
        addiu   $t0, $t0, %lo(QDATA)
        addiu   $t1, $t0, 72
1:      sd      $zero, 0($t0)
        addiu   $t0, $t0, 8
        bne     $t0, $t1, 1b
        nop
        jr      $ra
        li      $v0, -1

# qrec_send(a0 = APP ctx 0x550030): v0 = 0 nothing pending, 1 sent, < -1 send error (as 0x2b3b58).
# Kind 1 = u8 1, u8 HR 0x55c660(0x3e99c0), u16 quest 0x4166ac, 34 x (u8 i, u32 CNT[i+1]) (EU 0x2a3e00).
# Kind 2 = u8 2, u8 weapon class 0x5639f0(0x3e9ff4), u16 quest, u8 0, u32 CLEAR.
        .globl qrec_send
qrec_send:
        addiu   $sp, $sp, -80
        sd      $ra, 64($sp)
        sq      $s0, 0($sp)
        sq      $s1, 16($sp)
        sq      $s2, 32($sp)
        sq      $s3, 48($sp)
        move    $s0, $a0
        lui     $s1, %hi(QDATA)
        addiu   $s1, $s1, %lo(QDATA)
        li      $s2, 1
        addiu   $t0, $s1, 2
        addiu   $t1, $s1, 70
1:      lhu     $t2, 0($t0)             # any CNT[1..34]?
        bnez    $t2, 2f
        addiu   $t0, $t0, 2
        bne     $t0, $t1, 1b
        nop
        lw      $t2, Q_CLEAR($s1)
        beqz    $t2, 9f
        move    $v0, $zero
        li      $s2, 2
2:      lw      $t0, 20($s0)            # swap in BUF like state 52 (0x2b3940..0x2b3990)
        lw      $t1, 28($s0)
        lw      $t2, 32($s0)
        sw      $t0, Q_SAVE+0($s1)
        sw      $t1, Q_SAVE+4($s1)
        sw      $t2, Q_SAVE+8($s1)
        li      $t0, 176                # payload room: 184-byte BUF less the 8-byte header; kind 1 writes 174
        sw      $t0, 20($s0)
        addiu   $t1, $s1, Q_BUF
        sw      $t1, 28($s0)
        addiu   $t1, $t1, 8
        sw      $t1, 32($s0)
        move    $a0, $s0
        li      $a1, 53                 # request 6306's table entry: type 1, 0x63 0x06
        li      $a2, 1
        jal     0x2ae6c0
        move    $a3, $zero
        lw      $t0, 28($s0)
        li      $t1, 7
        sb      $t1, 4($t0)             # 0x6306 -> 0x6307
        move    $a0, $s0
        jal     0x2ae7e0                # u8 kind
        move    $a1, $s2
        li      $t0, 1
        bne     $s2, $t0, 3f
        lui     $a0, 0x3f
        jal     0x55c660                # Hunter Rank (sub_main)
        addiu   $a0, $a0, -26176        # 0x3e99c0
        b       4f
        nop
3:      jal     0x5639f0                # weapon class (sub_main)
        addiu   $a0, $a0, -24588        # 0x3e9ff4
4:      move    $a0, $s0
        jal     0x2ae7e0
        andi    $a1, $v0, 0xff
        lui     $t0, 0x41
        lhu     $a1, 0x66ac($t0)        # quest file number 0x4166ac
        jal     0x2ae850                # u16
        move    $a0, $s0
        li      $t0, 1
        bne     $s2, $t0, 6f
        move    $s3, $zero
5:      move    $a0, $s0
        jal     0x2ae7e0                # u8 monster index
        move    $a1, $s3
        sll     $t0, $s3, 1
        addu    $t0, $t0, $s1
        lhu     $a1, 2($t0)             # CNT[i + 1]
        jal     0x2ae8e0                # u32
        move    $a0, $s0
        addiu   $s3, $s3, 1
        sltiu   $t0, $s3, 34
        bnez    $t0, 5b
        nop
        b       7f
        nop
6:      move    $a0, $s0
        jal     0x2ae7e0                # u8 0
        move    $a1, $zero
        lw      $a1, Q_CLEAR($s1)
        jal     0x2ae8e0                # u32 seconds
        move    $a0, $s0
7:      lhu     $v1, 44($s0)            # big-endian payload length at buf+0, as 0x2b3b14
        lw      $v0, 28($s0)
        sll     $a0, $v1, 8
        srl     $v1, $v1, 8
        or      $v1, $a0, $v1
        sh      $v1, 0($v0)
        lw      $a2, 44($s0)
        lw      $a0, 16($s0)            # socket
        lw      $a1, 28($s0)
        jal     0x2167a0                # send(sock, buf, len + 8)
        addiu   $a2, $a2, 8
        lw      $t0, Q_SAVE+0($s1)
        lw      $t1, Q_SAVE+4($s1)
        lw      $t2, Q_SAVE+8($s1)
        sw      $t0, 20($s0)
        sw      $t1, 28($s0)
        sw      $t2, 32($s0)
        sw      $s2, Q_INFL($s1)
        slti    $t0, $v0, -1
        bnez    $t0, 9f
        nop
        li      $v0, 1
9:      ld      $ra, 64($sp)
        lq      $s0, 0($sp)
        lq      $s1, 16($sp)
        lq      $s2, 32($sp)
        lq      $s3, 48($sp)
        jr      $ra
        addiu   $sp, $sp, 80

# Reply table 0x369b08 (entry 54, the 6306 reply): 0x2b3ba0 -> h6306.
        .globl h6306
h6306:
        addiu   $sp, $sp, -32
        sd      $ra, 16($sp)
        sq      $s0, 0($sp)
        move    $s0, $a0
        jal     0x221660                # FlushCache(2): the new loop bound at 0x2b4c4c is fetched
        li      $a0, 2                  # before the 6307 reply even right after an install
        jal     0x2b3ba0                # original: ctx+60 = 1, state 50 (6320), -59 on an error reply
        move    $a0, $s0
        bnez    $v0, 9f
        nop
        jal     qrec_send
        move    $a0, $s0
        blez    $v0, 9f                 # 0: 6320 goes out as before; < 0: error
        li      $t0, 43
        sb      $t0, 1($s0)             # wait for the 6307 reply
        move    $v0, $zero
9:      ld      $ra, 16($sp)
        lq      $s0, 0($sp)
        jr      $ra
        addiu   $sp, $sp, 32

# Reply table entry 67 (new, 0x369b3c): the 6307 reply.
        .globl h6307
h6307:
        addiu   $sp, $sp, -32
        sd      $ra, 16($sp)
        sq      $s0, 0($sp)
        move    $s0, $a0
        lw      $t0, 36($a0)            # received frame
        lb      $t1, 6($t0)
        li      $t2, -1
        bne     $t1, $t2, 1f
        lui     $t3, %hi(QDATA)
        b       9f
        li      $v0, -59                # error reply, as 0x2b3bc4
1:      addiu   $t3, $t3, %lo(QDATA)
        lw      $t1, Q_INFL($t3)
        li      $t2, 1
        bne     $t1, $t2, 3f
        addiu   $t4, $t3, 72
2:      sd      $zero, 0($t3)           # kind 1 acked: clear the counts
        addiu   $t3, $t3, 8
        bne     $t3, $t4, 2b
        nop
        b       4f
        nop
3:      sw      $zero, Q_CLEAR($t3)     # kind 2 acked
4:      jal     qrec_send
        move    $a0, $s0
        bltz    $v0, 9f
        nop
        bnez    $v0, 5f
        nop
        jal     0x2b3ba0                # nothing left: ctx+60 = 1, state 50 -> 6320
        move    $a0, $s0
        b       9f
        nop
5:      li      $t0, 43
        sb      $t0, 1($s0)
        move    $v0, $zero
9:      ld      $ra, 16($sp)
        lq      $s0, 0($sp)
        jr      $ra
        addiu   $sp, $sp, 32
