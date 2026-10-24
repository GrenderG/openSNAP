"""Build and inspect DNAS2 install blobs from a game's `dnas_ins` overlay.

A valid install blob is `[0x580-byte DNAS install header][payload]`. Addresses
are in Monster Hunter EU's `dnas_ins` (base `0xa86200`). The client's
`sceDNAS2InstPersonalizeDataLength_nohdd` / `…Data_nohdd` (`0xaa67f8` /
`0xaa6848`) run the worker `0xaa3430`, which reads 0x580 bytes first. A
shorter download returns the bytes read without writing an output length
(`0xaa34cc`-`0xaa34dc`), so `InetDNAS2PersonalEncode` (`0xaafbd0`) returns 0
("still running") and the game retries forever: the install hang MH EU hits
with the raw 0x408-byte `M-HUNTER` patch.

The header's records are RSA-1024 signatures. Record 1 at header +0x300 is
checked with key 83 by `0xaacc48`: `c^65537 mod n`, PKCS#1 type-1 unpadding
(error 524 on failure), then the token `791253d95296b82c` at plaintext +0x20
(else -10201). Its first 32 bytes are the key and IV of a 16-byte-block CBC
cipher (`0xaaf3d0`, RC6-32/20/16 in CBC mode) that decrypts header +0x500, whose bytes 4
and 5 select the keys of records 2 (+0x280) and 3 (+0x000). The public keys
sit in the overlay key table at `0xab5be0`; the private keys are not on the
client, so a header cannot be made here: `build_install_packet` only frames a
payload behind a genuine header you supply, taken from a retail install
download. Data calls the RSA check at five sites (`0xaa4870` reads the key
ids): records 1, 3 and 2 at `0xaa45b0`, `0xaa49c0` and `0xaa4af0`, then two
more while it processes the payload, keyed by bytes 0 and 2 of +0x500
(`0xaa4e30`, `0xaa55d4`; ids 91 and 95 in Capcom's EU patch). The card
decode (`0xaafd30`) never uses those two keys, and their moduli have not been
extracted.

The payload is signed too, so a genuine header only works with the payload it
was signed with. Capcom's EU patch as stored on the memory card (0x1f70 bytes,
read back by `InetDNAS2PersonalDecode` `0xaafd30`) shows how:

- the header, whose record at +0x180 is the only console-bound part (the
  session key and IV, under a key made from the i.LINK ID);
- the payload at 0x580..0x1ef0, encrypted under that session key; the patch
  itself is further encrypted under a key held in RSA record +0x100;
- an RSA record at 0x1ef0 (key 0x50, token `d9c2a497b1124e34`), left
  unencrypted, holding the digest of the payload's encrypted patch region
  0x13f0..0x1ee0 (after the session layer is removed). `0xaa20f0` compares all
  20 bytes with `sha1_dnas` of that region and fails with -10202.

Changing one byte of the patch therefore needs a new 0x1ef0 record, signed by
Sony. A genuine blob can be moved to another console by rewriting +0x180, but
not changed. The layout above comes from the signed record +0x280, so other
patches may have other lengths and offsets. `digest_record` checks a 0x1ef0
record with key 0x50 and returns its digest.

The overlay decrypts its own code at run time, so a dump from the disc must be
decrypted first. Each guarded block has an inline descriptor
`{flag, d4, d8, d12}` passed to a decryptor (`0xaa6d18` / `0xaaf018` / `0xa88a28`): length
`d4 ^ d12`, key id `d8 >> 1` (a list of words to skip), `(d4 >> 28) & 7`
passes (0 means 5), and each pass applies the word op chosen by a nibble of
`d4`: xor `0x09f8ed17`, xor `0xa95128c3`, rotate right 13, rotate right 8,
byte order 2,0,3,1, or one of three combinations of these.

CLI:
    python3 -m tools.dnas.install_packet --overlay dnas_ins.bin \
        --header header.bin --payload in.bin -o out.bin
    python3 -m tools.dnas.install_packet --overlay dnas_ins.bin --inspect
"""

import argparse
import struct
import sys

# The DNAS install header the library's reader (`0xaa68f8`) demands before
# the payload.
DNAS_HEADER_LEN = 0x580
# Overlay load address and the install key table, constant across titles
# (`0xaa3430` etc.).
OVERLAY_BASE = 0xa86200
# The record-key table: 30 entries of 200 bytes at `0xab5be0 + 200*cmd`
# (`0xaaca50`), selected by cmd = keyid - 70 for key ids 70..99 (`0xaacc48`
# range check).
KEY_TABLE_VA = 0xab5be0
KEY_ENTRY_STRIDE = 200
KEY_ENTRY_COUNT = 30
# The ASCII tokens the decrypted records must contain, or the worker returns
# -10201.
REQUIRED_TOKENS = (
    b'791253d95296b82c',
    b'cd218709bfd73b9f',
    b'97c03eeb23cb1287',
    b'1d9195a07aa9969f',
)


# The stored EU patch's payload signature: an RSA record (key id, token) at
# DIGEST_RECORD_OFF over the region DIGEST_REGION (`0xaa1f74` - `0xaa20f0`).
DIGEST_RECORD_OFF = 0x1ef0
DIGEST_RECORD_LEN = 0x80
DIGEST_KEY_ID = 0x50
DIGEST_TOKEN = b'd9c2a497b1124e34'
DIGEST_REGION = (0x13f0, 0x1ee0)
# The modulus of key 0x50 (e = 65537), as `0xaacc48` unwraps it from the key
# table at run time (read from the overlay's work buffer in an emulated
# decode of Capcom's EU patch; it recovers that patch's record).
DIGEST_KEY_N = int(
    'ec04f86e8f755a7e7c14c390d651e3a5e62a39565bf692bb3e9d0a113f0519fd'
    '57c91ae823dc1f6adc2e9374cdcf9675658aa3ec1b9f3ab0544f11dbcd7348da'
    'f5892639cee54418bab52ff189897007bd799f58124144a0ae4d7d97a2066b6e'
    'd90af5f4d7dbfbf78b8634b6910ac8af089853b276e93ac4cb23c41dc8ee7d09', 16)


class InstallBlobError(Exception):
    """Raised when an install blob cannot be built or an overlay looks wrong."""


class OverlayInfo:
    """Facts read out of a `dnas_ins` overlay image."""

    def __init__(self, size, decrypted, token_offsets, key_table_offset):
        self.size = size
        self.decrypted = decrypted
        self.token_offsets = token_offsets
        self.key_table_offset = key_table_offset

    def __repr__(self):
        toks = ', '.join('%s@0x%x' % (t.decode(), o) for t, o in self.token_offsets.items())
        return ('OverlayInfo(size=0x%x, decrypted=%s, key_table_offset=0x%x, tokens=[%s])'
                % (self.size, self.decrypted, self.key_table_offset, toks))


def read_overlay(overlay_bytes):
    """Inspect an overlay and return an `OverlayInfo`.

    The overlay must already be plaintext (see the module docstring); a raw
    self-encrypted image hides the tokens, which is what `decrypted` reports.
    """
    data = bytes(overlay_bytes)
    token_offsets = {}
    for token in REQUIRED_TOKENS:
        idx = data.find(token)
        if idx >= 0:
            token_offsets[token] = idx
    decrypted = len(token_offsets) == len(REQUIRED_TOKENS)
    key_table_offset = KEY_TABLE_VA - OVERLAY_BASE
    return OverlayInfo(len(data), decrypted, token_offsets, key_table_offset)


def key_table_entry(overlay_bytes, cmd):
    """Return the raw 200-byte record-key table entry for `cmd` (keyid = cmd + 70).

    Each entry is obfuscated in the image; `0xaaca50 -> 0xaab9e0` unwraps it
    into the DER RSA-1024 public key (e = 65537) that verifies a record. The
    unwrap is not reimplemented here.
    """
    if not 0 <= cmd < KEY_ENTRY_COUNT:
        raise InstallBlobError('cmd must be in 0..%d' % (KEY_ENTRY_COUNT - 1))
    off = (KEY_TABLE_VA - OVERLAY_BASE) + KEY_ENTRY_STRIDE * cmd
    if off + KEY_ENTRY_STRIDE > len(overlay_bytes):
        raise InstallBlobError('overlay too small for key table')
    return overlay_bytes[off:off + KEY_ENTRY_STRIDE]


def _sha1_block(h, block):
    w = list(struct.unpack('>16I', block))
    for i in range(16, 80):
        x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]
        w.append(((x << 1) | (x >> 31)) & 0xffffffff)
    a, b, c, d, e = h
    for i in range(80):
        if i < 20:
            f, k = (b & c) | (~b & d), 0x5a827999
        elif i < 40:
            f, k = b ^ c ^ d, 0x6ed9eba1
        elif i < 60:
            f, k = (b & c) | (b & d) | (c & d), 0x8f1bbcdc
        else:
            f, k = b ^ c ^ d, 0xca62c1d6
        t = (((a << 5) | (a >> 27)) + (f & 0xffffffff) + e + k + w[i]) & 0xffffffff
        a, b, c, d, e = t, a, ((b << 30) | (b >> 2)) & 0xffffffff, c, d
    return [(x + y) & 0xffffffff for x, y in zip(h, (a, b, c, d, e))]


def sha1_dnas(data):
    """Return the library's SHA-1 (`0xaac2b8`) of `data`.

    It differs from standard SHA-1 only when the last partial block is 48..55
    bytes: those get the 0x80 pad and zeros, and the bit length goes in an
    extra block (standard SHA-1 does that only from 56 bytes). The EU patch's
    0xaf0-byte region is such a case.
    """
    data = bytes(data)
    h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0]
    end = len(data) - len(data) % 64
    for i in range(0, end, 64):
        h = _sha1_block(h, data[i:i + 64])
    tail = data[end:] + b'\x80'
    if len(tail) > 48:
        h = _sha1_block(h, tail + bytes(64 - len(tail)))
        tail = b''
    h = _sha1_block(h, tail + bytes(56 - len(tail)) + struct.pack('>Q', 8 * len(data)))
    return struct.pack('>5I', *h)


def digest_record(record):
    """Return the 20-byte digest signed in a 0x1ef0 record (`0xaacc48`).

    `record` is the raw 0x80 bytes. Raises `InstallBlobError` unless it is a
    valid signature: PKCS#1 type-1 padding under key 0x50, then the digest and
    `DIGEST_TOKEN`. A blob is genuine when this equals `sha1_dnas` of its
    region `DIGEST_REGION` with the session layer removed.
    """
    if len(record) != DIGEST_RECORD_LEN:
        raise InstallBlobError('record must be 0x%x bytes' % DIGEST_RECORD_LEN)
    m = pow(int.from_bytes(record, 'big'), 65537, DIGEST_KEY_N).to_bytes(128, 'big')
    sep = m.find(0, 2)
    body = m[sep + 1:]
    if (m[:2] != b'\x00\x01' or sep < 10 or set(m[2:sep]) != {0xff}
            or body[20:20 + len(DIGEST_TOKEN)] != DIGEST_TOKEN):
        raise InstallBlobError('not a record signed with key 0x%x' % DIGEST_KEY_ID)
    return body[:20]


def build_install_packet(overlay_bytes, payload, header=None):
    """Return `[0x580 header][payload]` for the given overlay.

    `overlay_bytes` selects the title (keys/tokens are read from it, never
    hardcoded). `header` must be a genuine, signed 0x580-byte DNAS install
    header; one cannot be made from the overlay because its records are RSA
    signatures (module docstring). `payload` must be the one the header's blob
    was signed with: the payload is signed too.
    """
    info = read_overlay(overlay_bytes)
    if not info.decrypted:
        raise InstallBlobError(
            'overlay is still self-encrypted; decrypt it first (see the module docstring)')
    if header is None:
        raise InstallBlobError(
            'a signed DNAS install header cannot be generated; '
            'pass a genuine 0x580-byte header via header=')
    if len(header) != DNAS_HEADER_LEN:
        raise InstallBlobError('header must be exactly 0x%x bytes, got 0x%x'
                               % (DNAS_HEADER_LEN, len(header)))
    return bytes(header) + bytes(payload)


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Build or inspect a DNAS2 install blob.')
    parser.add_argument('--overlay', required=True, help='decrypted dnas_ins overlay image')
    parser.add_argument('--payload', help='payload to wrap')
    parser.add_argument('--header', help='0x580-byte DNAS install header for this title')
    parser.add_argument('-o', '--output', help='output install blob')
    parser.add_argument('--inspect', action='store_true', help='print overlay facts and exit')
    args = parser.parse_args(argv)

    with open(args.overlay, 'rb') as handle:
        overlay = handle.read()

    if args.inspect:
        info = read_overlay(overlay)
        print(info)
        print('key table: %d entries of %d bytes at va 0x%x (key ids 70..99)'
              % (KEY_ENTRY_COUNT, KEY_ENTRY_STRIDE, KEY_TABLE_VA))
        return 0

    if not args.payload or not args.output:
        parser.error('--payload and --output are required unless --inspect is given')
    with open(args.payload, 'rb') as handle:
        payload = handle.read()
    header = None
    if args.header:
        with open(args.header, 'rb') as handle:
            header = handle.read()
    try:
        blob = build_install_packet(overlay, payload, header=header)
    except InstallBlobError as error:
        print('error:', error, file=sys.stderr)
        return 1
    with open(args.output, 'wb') as handle:
        handle.write(blob)
    print('wrote 0x%x bytes to %s' % (len(blob), args.output))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
