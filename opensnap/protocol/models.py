"""Protocol data models."""

from dataclasses import dataclass

from opensnap.protocol.constants import FOOTER_BYTES

WIRE_FORMAT_SNAP = 'snap'
WIRE_FORMAT_AM_BETA1_LEGACY = 'automodellista_beta1_legacy'


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
    embedded_in_multi: bool = False
    footer_bytes: bytes = FOOTER_BYTES
    wire_format: str = WIRE_FORMAT_SNAP
