"""World-list protocol constants shared by `service` and `ram_patch`.

A leaf module, so `ram_patch` can use them without `service` importing data
back out of `ram_patch`.
"""

# The 6502 (World status) reply body, parsed by the handler at EU `0x002a2420`
# as `u8 flag` + two big-endian `u32`s (1 + 4 + 4 = 9 bytes):
#   [0]   flag=1 records this World (loads its Lands); 0 skips to the next
#         World, anything else is a protocol error (`bne` at `0x002a25d0`).
#   [1:5] stored to session global `0x0050f038` (`0x002a25ec`); on entering the
#         post-status state it arms the frame countdown at `0x003537c4`
#         (`0x00236b94`), decremented each frame and clamped at zero
#         (`0x00102498`).  No code in this EU build reads the timer or acts on
#         its expiry, so the value is inert here; shipped as 1.
#   [5:9] an opaque per-World token stored in the World record at `+0x17c`
#         (`0x002a261c`) and echoed back big-endian in a later frame.  Shipped
#         as 0.
# Both words are byte-swapped on read (reader `0x0029e6a0`), i.e. big-endian.
WORLD_STATUS_REPLY = b'\x01\x00\x00\x00\x01\x00\x00\x00\x00'

# The client's World array holds 16 records and its 6503 handler only accepts
# `total < 17`, so record 16 cannot be paged in a first `start = 0` page.  A
# normal 8-record first page moves the client's cursor to 8; the next request
# is answered with a 9-record page starting at 8 whose last record is 16.
WORLD_PAGE_SIZE = 8
FINAL_PAGE_START = WORLD_PAGE_SIZE
FINAL_PAGE_COUNT = WORLD_PAGE_SIZE + 1
