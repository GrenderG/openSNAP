"""Monster Hunter APP TCP framing and field codec."""

APP_FRAME_HEADER_SIZE = 8
APP_FRAME_MAX_SIZE = 1024 * 1024
APP_FIELD_MASK = b'MINAMIOH'

APP_DIRECTION_SERVER = 1
APP_DIRECTION_REPLY = 2
# Server push the client did not request (6001, 6210, 1004).
APP_DIRECTION_PUSH = 0x10


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
