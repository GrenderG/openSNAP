"""Payload parsing and packing helpers."""

import struct


def get_c_string(payload: bytes, offset: int) -> str:
    """Read a null-terminated string from payload."""

    if offset >= len(payload):
        return ''

    terminator = payload.find(b'\x00', offset)
    if terminator == -1:
        terminator = len(payload)
    return payload[offset:terminator].decode('utf-8', errors='ignore')


def get_u32(payload: bytes, offset: int) -> int:
    """Read a big-endian uint32 from payload."""

    return struct.unpack_from('>L', payload, offset)[0]


def get_u16(payload: bytes, offset: int) -> int:
    """Read a big-endian uint16 from payload."""

    return struct.unpack_from('>H', payload, offset)[0]


def get_u8(payload: bytes, offset: int) -> int:
    """Read a uint8 from payload."""

    return struct.unpack_from('>B', payload, offset)[0]


def get_len_prefixed_string(payload: bytes, offset: int) -> str:
    """Read a 16-bit-length-prefixed string from payload."""

    size = get_u16(payload, offset)
    start = offset + 2
    end = start + size
    return payload[start:end].decode('utf-8', errors='ignore')
