"""Protocol data models."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Network endpoint."""

    host: str
    port: int


@dataclass(slots=True)
class SnapMessage:
    """Decoded or to-be-encoded SNAP message."""

    endpoint: Endpoint
    type_flags: int
    packet_number: int
    command: int
    session_id: int
    sequence_number: int
    acknowledge_number: int
    payload: bytes = b''
    size_word_override: int | None = None
