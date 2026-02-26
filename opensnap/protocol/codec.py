"""SNAP datagram encoder and decoder."""

from collections.abc import Sequence
import struct

from opensnap.protocol.constants import (
    FLAG_MULTI,
    FOOTER_BYTES,
    FOOTER_SIZE,
    HEADER_SIZE,
    LENGTH_MASK,
    TYPE_MASK,
)
from opensnap.protocol.models import Endpoint, SnapMessage


class PacketDecodeError(ValueError):
    """Raised when a datagram cannot be decoded."""


def decode_datagram(data: bytes, endpoint: Endpoint) -> list[SnapMessage]:
    """Decode a UDP datagram into one or more SNAP messages."""

    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise PacketDecodeError('Datagram is too small.')

    if data[-FOOTER_SIZE:] != FOOTER_BYTES:
        raise PacketDecodeError('Datagram footer marker is missing.')

    messages: list[SnapMessage] = []
    offset = 0
    payload_limit = len(data) - FOOTER_SIZE

    multi_command_seen = False
    while offset < payload_limit:
        if offset + HEADER_SIZE > payload_limit:
            raise PacketDecodeError('Message header crosses datagram boundary.')

        size_word, packet_and_command, session_id, seq, ack = struct.unpack_from('>2H3L', data, offset)
        # High bits carry type flags and low bits carry encoded message length.
        type_flags = size_word & TYPE_MASK
        encoded_length = size_word & LENGTH_MASK

        if encoded_length < HEADER_SIZE:
            raise PacketDecodeError(f'Invalid message length: {encoded_length}.')

        next_offset = offset + encoded_length
        if next_offset > payload_limit:
            raise PacketDecodeError('Message length exceeds datagram body.')

        packet_number = (packet_and_command >> 8) & 0xFF
        command = packet_and_command & 0xFF
        payload = data[offset + HEADER_SIZE:next_offset]

        message = SnapMessage(
            endpoint=endpoint,
            type_flags=type_flags,
            packet_number=packet_number,
            command=command,
            session_id=session_id,
            sequence_number=seq,
            acknowledge_number=ack,
            payload=payload,
            size_word_override=size_word if type_flags & FLAG_MULTI else None,
            embedded_in_multi=multi_command_seen,
        )
        messages.append(message)

        offset = next_offset

        # Incoming multi datagrams are command-dependent:
        # - 0x0f (kkSend) may embed a follow-up command (for example room leave 0x07)
        #   that must be dispatched.
        # - other observed multi commands should keep snapsi-compatible "first entry only"
        #   behavior to avoid over-dispatching embedded query entries.
        if type_flags & FLAG_MULTI:
            multi_command_seen = True
        if (type_flags & FLAG_MULTI) and command != 0x0F:
            break

    return messages


def encode_messages(messages: Sequence[SnapMessage]) -> bytes:
    """Encode one or more SNAP messages into a datagram."""

    if not messages:
        raise ValueError('At least one message is required.')

    encoded = bytearray()
    for message in messages:
        if message.size_word_override is None:
            # For regular packets, length is header plus payload length.
            size_word = message.type_flags | (len(message.payload) + HEADER_SIZE)
        else:
            # Multi-message payloads already carry a precomputed size word.
            size_word = message.size_word_override

        packet_and_command = ((message.packet_number & 0xFF) << 8) | (message.command & 0xFF)
        encoded.extend(
            struct.pack(
                '>2H3L',
                size_word,
                packet_and_command,
                message.session_id,
                message.sequence_number,
                message.acknowledge_number,
            )
        )
        encoded.extend(message.payload)

    encoded.extend(FOOTER_BYTES)
    return bytes(encoded)
