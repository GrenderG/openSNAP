"""Capcom APP TCP framing, field codec and session commands.

Capcom's SN@P titles open short TCP connections to
`app01.reo.capcom.sf.yav4.com:10127` beside their SN@P session. The protocol is
Capcom's own: the client code lives outside the kkClient SDK (Outbreak
`0x001f3e00..0x001f7400`) and only uses plain socket calls. Monster Hunter
(`SLUS_208.96`, `SLES_527.07`) and Resident Evil Outbreak (`SLUS_207.65`) share
the frame header, the field codec, the session commands and the 1002 answer
below; everything else is per title.

Frame: `u16 payload length, u8 direction, u16 command, u8 sequence, u8 error,
u8 tail`, all big-endian (Outbreak builder `0x001f4a10`). Clients send
requests with direction 1 and their own sequence counter and answer server
requests with direction 2, echoing the command and sequence.

Field: `u16 length + 2, u16 checksum, masked bytes`, masked with
`MINAMIOH` (Outbreak keeps it as base64 `TUlOQU1JT0g=` at `0x00259b10`) and the
frame sequence (Outbreak encode `0x001f43c0`, decode `0x001f44c0`). Outbreak
also adds the `u16` of the server's 1002 to the sequence; servers send 0.
"""

from dataclasses import dataclass

APP_FRAME_HEADER_SIZE = 8
APP_FRAME_MAX_SIZE = 1024 * 1024
APP_FIELD_MASK = b'MINAMIOH'

APP_DIRECTION_SERVER = 1
APP_DIRECTION_REPLY = 2
# Server push the client did not request (6001, 6210, 1004).
APP_DIRECTION_PUSH = 0x10

# Session commands with the same meaning in every title.
# - 1002 opens a connection from the server; its payload is the `u16` field
#   salt above. The client answers with its login name, build and version.
# - 1004 closes it: the client sends it when done and waits for the reply.
# - 6001 is the server's client patch offer (a push); `u8 0` offers no patch
#   (MH NA `0x002b4150`, MH EU `0x002a46f8`, Outbreak `0x001f512c`).
CMD_APP_CONNECT = 0x1002
CMD_APP_COMPLETE = 0x1004
CMD_APP_VERSION = 0x6001
CONNECT_NO_SALT = b'\x00\x00'
VERSION_NO_PATCH = b'\x00'


class AppCodecError(ValueError):
    """Raised when one APP frame or encoded field is malformed."""


def app_frame(
    direction: int,
    command: int,
    *,
    sequence: int = 0,
    error: int = 0,
    tail: int = 0xFF,
    payload: bytes = b'',
) -> bytes:
    """Build one frame: `u16 payload_len, u8 direction, u16 command, u8 seq, u8 error, u8 tail`."""

    return (
        len(payload).to_bytes(2, 'big')
        + bytes([direction & 0xFF])
        + command.to_bytes(2, 'big')
        + bytes([sequence & 0xFF, error & 0xFF, tail & 0xFF])
        + payload
    )


def frame_command(frame: bytes) -> int:
    return int.from_bytes(frame[3:5], 'big')


def frame_sequence(frame: bytes) -> int:
    return frame[5]


def frame_payload(frame: bytes) -> bytes:
    return frame[APP_FRAME_HEADER_SIZE:]


def pop_app_frame(buffer: bytearray) -> bytes | None:
    """Consume exactly one complete frame and keep any coalesced bytes."""

    if len(buffer) < APP_FRAME_HEADER_SIZE:
        return None
    total = APP_FRAME_HEADER_SIZE + int.from_bytes(buffer[:2], 'big')
    if total > APP_FRAME_MAX_SIZE:
        raise AppCodecError('APP frame too large.')
    if len(buffer) < total:
        return None
    frame = bytes(buffer[:total])
    del buffer[:total]
    return frame


def split_app_frames(data: bytes) -> list[bytes]:
    """Split a complete byte stream into frames."""

    buffer = bytearray(data)
    frames = []
    while (frame := pop_app_frame(buffer)) is not None:
        frames.append(frame)
    if buffer:
        raise AppCodecError('Trailing bytes after the last APP frame.')
    return frames


def encode_app_field(plain: bytes, sequence: int) -> bytes:
    """Encode one field: `u16 length+2, u16 checksum, masked bytes`."""

    encoded = bytes(
        plain[index] ^ APP_FIELD_MASK[index & 7] ^ ((sequence + index) & 0xFF)
        for index in range(len(plain))
    )
    checksum = (sum(plain) + sequence * 0x101) & 0x7FFF
    return (len(encoded) + 2).to_bytes(2, 'big') + checksum.to_bytes(2, 'big') + encoded


def decode_app_field(field: bytes, sequence: int) -> tuple[bytes, int]:
    """Decode one field and return `(plain, consumed_size)`."""

    if len(field) < 4:
        raise AppCodecError('APP field is truncated.')
    total = 2 + int.from_bytes(field[:2], 'big')
    if total < 4 or total > len(field):
        raise AppCodecError('APP field length is invalid.')
    encoded = field[4:total]
    plain = bytes(
        encoded[index] ^ APP_FIELD_MASK[index & 7] ^ ((sequence + index) & 0xFF)
        for index in range(len(encoded))
    )
    if int.from_bytes(field[2:4], 'big') != (sum(plain) + sequence * 0x101) & 0x7FFF:
        raise AppCodecError('APP field checksum mismatch.')
    return plain, total


@dataclass(frozen=True, slots=True)
class ConnectAnswer:
    """The client's answer to the server's 1002, which tells the titles apart.

    Payload: `field login, u8 build, u8 flags, field version, 4 x u8`, the same
    in every title (Outbreak builder `0x001f4e30`, MH NA `0x002aeb40`). The
    build byte is fixed per title: Outbreak 1 (`netwk.bin` `0x00608f0c`,
    `nethttp.bin` `0x0073482c`), MH NA 2 (`0x00247fc8`), MH EU 3. The version
    is the client's build stamp, `YYMMDDhhmm` of its own `__DATE__`/`__TIME__`
    (Outbreak `0x001bb438`, MH NA `0x0010fe00`), which only a downloaded patch
    replaces.
    """

    login: bytes
    build: int
    flags: int
    version: bytes


def read_connect_answer(frame: bytes) -> ConnectAnswer:
    """Parse the client's 1002 answer frame."""

    if frame_command(frame) != CMD_APP_CONNECT:
        raise AppCodecError(f'expected the 0x{CMD_APP_CONNECT:04x} answer, got 0x{frame_command(frame):04x}')
    payload = frame_payload(frame)
    login, size = decode_app_field(payload, frame_sequence(frame))
    if len(payload) < size + 2:
        raise AppCodecError('the 0x1002 answer has no build byte')
    version, _ = decode_app_field(payload[size + 2:], frame_sequence(frame))
    return ConnectAnswer(login=login, build=payload[size], flags=payload[size + 1], version=version)
