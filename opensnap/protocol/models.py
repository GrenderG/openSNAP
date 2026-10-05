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
    # Top-level SNAP packets use this trailing header word as real transport ACK
    # state. In both release and beta1, the client assigns ACK ownership from
    # the outer reliable packet before `kkRUDPTopMultiMessageHandle` dispatches
    # embedded children, and the multi-child `kkSetMessage` path does not
    # initialize child header `+0x0c`. So `embedded_in_multi=True` exposes raw
    # copied/reused header bytes here, not a meaningful transport ACK.
    acknowledge_number: int
    payload: bytes = b''
    size_word_override: int | None = None
    embedded_in_multi: bool = False
    # Outbound-only header session id override. `session_id` always names the
    # recipient connection that owns sequence allocation, reliable tracking,
    # and ACK retirement: clients keep one RUDP window per server connection
    # (`kkReceiveExtentCheck` never reads the header session word) and build
    # bare ACKs from their own connection id (`SLUS_206.42` `0x002eac64`,
    # `SLUS_208.96` `0x001fe224`). The header word itself is surfaced to game
    # callbacks as the packet source (`kkDispatchingOperation` info `+8`:
    # `SLUS_206.42` `0x002ed0e0`, `SLUS_208.96` `0x00201d90`), so peer relays
    # that must identify their originator set it here.
    source_session_id: int | None = None
    footer_bytes: bytes = FOOTER_BYTES
    wire_format: str = WIRE_FORMAT_SNAP
