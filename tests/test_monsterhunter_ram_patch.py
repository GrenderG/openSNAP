"""RAM-only patch delivery for retail Monster Hunter EU and NA (`ram_patch.py`)."""

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from opensnap.config import default_app_config
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA
from opensnap_app.capcom.monsterhunter.constants import (
    FINAL_PAGE_COUNT,
    FINAL_PAGE_START,
    WORLD_PAGE_SIZE,
    WORLD_STATUS_REPLY,
)
from opensnap_app.capcom.monsterhunter.ram_patch import (
    APP_BODY_LIMIT,
    BLOB_MAGIC,
    CHUNK_SIZE,
    DOWNLOAD_INDEX,
    DOWNLOAD_LIMIT,
    EU_DESCRIPTION_BODY,
    EU_DESCRIPTION_FIELD_SIZE,
    EU_DESCRIPTION_FRAME_HEADER,
    HANDLER_6502_REPLY,
    NA_APPLIER,
    NA_CHUNK_SIZE,
    NA_DESCRIPTION_BODY,
    NA_DESCRIPTION_BODY_SIZE,
    NA_DESCRIPTION_FIELD_SIZE,
    NA_DESCRIPTION_FRAME_HEADER,
    NA_DESCRIPTION_LENGTH,
    NA_DESCRIPTION_NAME,
    NA_DESCRIPTION_SIZE,
    NA_DESCRIPTION_TABLE,
    NA_DESCRIPTION_VERSION,
    NA_DOWNLOAD_BASE,
    NA_DOWNLOAD_INDEX,
    NA_DOWNLOAD_INDEX_OFFSET,
    NA_DOWNLOAD_OFFSET_OFFSET,
    NA_FLUSH_CACHE,
    NA_GUARD,
    NA_HANDLER_6502,
    NA_HANDLER_6502_REPLY,
    NA_LOGIN_BLOCK,
    NA_NAME,
    NA_OVERLAY_ID,
    NA_RECORD_DESCRIPTION,
    NA_UNCACHED_BLOB,
    NA_WINDOW,
    NA_WRITE_BASE,
    RESIDENT_BLOB,
    TOTAL_RECORDS,
    WRITE_BASE,
    RamPatchPageTooLarge,
    build_na_ram_patch,
    build_na_shellcode,
    build_ram_patch,
    build_shellcode,
    handler_offset,
    load_ram_patch,
    offset_to,
    parse_patch_blob,
)
from opensnap_app.capcom.monsterhunter.service import (
    NA_RAM_PATCH_ENVIRONMENT_KEY,
    RAM_PATCH_ENVIRONMENT_KEY,
    read_app_service_config,
)
from opensnap_app.capcom.protocol import (
    APP_FIELD_MASK,
    decode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
)

DATA_DIRECTORY = Path(__file__).resolve().parents[1] / 'data' / 'monsterhunter' / 'patches'
CUSTOM_BLOB = (DATA_DIRECTORY / 'eu_custom_1002.bin').read_bytes()
OFFICIAL_BLOB = (DATA_DIRECTORY / 'eu_official_1001.bin').read_bytes()
NA_BLOB = (DATA_DIRECTORY / 'na_custom_1002.bin').read_bytes()
UNCACHED_BLOB = 0x20000000 + RESIDENT_BLOB
WORLDS = [('world-a.example', 'World A', 'First world'), ('world-b.example', 'World B', 'Second world')]
SEQUENCE = 9


def records(
    payload: bytes, sequence: int, *, start: int = 0, count: int = 17
) -> tuple[int, list[tuple[bytes, bytes, bytes, bytes]]]:
    """Decode a 6503 reply into its total and `(key, name, 8 raw bytes, description)` records."""

    total, page_start, page_count = struct.unpack_from('>2HB', payload)
    assert (page_start, page_count) == (start, count)
    offset = 5
    decoded = []
    for _ in range(count):
        key, size = decode_app_field(payload[offset:], sequence)
        offset += size
        name, size = decode_app_field(payload[offset:], sequence)
        offset += size
        raw = payload[offset:offset + 8]
        offset += 8
        description, size = decode_app_field(payload[offset:], sequence)
        offset += size
        decoded.append((key, name, raw, description))
    assert offset == len(payload)
    return total, decoded


def priming_sequence_byte(sequence: int, offset: int) -> int:
    """The byte the field decoder reads back as `sequence` from the header slot."""

    return sequence ^ APP_FIELD_MASK[offset & 7] ^ ((sequence + offset) & 0xFF)


def client_description(payload: bytes, sequence: int, seq_offset: int) -> bytes:
    """Decode record 16's description exactly as the client's field decoder does.

    The client copies the field into the receive buffer, then reads the frame
    header's sequence byte from that buffer (which the copy has just overwritten
    with the field's own byte) to key the checksum.  Reading the key from the
    wire field is therefore the behavior the client sees.
    """

    field = payload[-(4 + 255):]
    total = int.from_bytes(field[:2], 'big')
    checksum = int.from_bytes(field[2:4], 'big')
    assert total == 2 + 255
    data = field[4:]
    key = data[seq_offset]
    plain = bytes(
        data[index] ^ APP_FIELD_MASK[index & 7] ^ ((key + index) & 0xFF)
        for index in range(255)
    )
    assert key == sequence, f'the client would decode the description with {key:#04x}'
    assert (sum(plain) + key * 0x101) & 0x7FFF == checksum
    return plain


def download(frame: bytes) -> tuple[int, int, bytes]:
    """Decode a 6204 frame into `(index, offset, data)`."""

    assert frame_command(frame) == 0x6204
    payload = frame_payload(frame)
    index, offset = struct.unpack_from('>HL', payload)
    data, size = decode_app_field(payload[6:], frame_sequence(frame))
    assert 6 + size == len(payload)
    return index, offset, data


def na_patch(frame: bytes) -> tuple[bytes, int, bytes]:
    """Decode a 6002 frame into `(name, offset, data)`."""

    assert frame_command(frame) == 0x6002
    payload = frame_payload(frame)
    name, size = decode_app_field(payload, frame_sequence(frame))
    offset, = struct.unpack_from('>I', payload, size)
    data, data_size = decode_app_field(payload[size + 4:], frame_sequence(frame))
    assert size + 4 + data_size == len(payload)
    return name, offset, data


def priming_key(key: bytes) -> tuple[int, int, int]:
    """The `(index, offset, limit)` words of a priming record's key field."""

    assert key[:1] == b'\x00'
    return key[1], struct.unpack_from('<I', key, 4)[0], struct.unpack_from('<H', key, 10)[0]


class RamPatchBlobTests(unittest.TestCase):
    def test_custom_blob_has_the_built_entries(self) -> None:
        blob = parse_patch_blob(CUSTOM_BLOB)
        self.assertEqual(len(blob.entries), 17)
        self.assertEqual(blob.entries[0].key, 0xFFFFFFFF)
        self.assertEqual(blob.entries[0].length, 1141)
        self.assertIsNone(blob.entries[0].source)
        # Capcom's lobby hook and the post-quest hook are re-aimed at our copy.
        self.assertEqual(
            [(entry.key, entry.destination, entry.data.hex()) for entry in blob.entries[1:3]],
            [(0x23, 0x5784D0, 'c6780e0800000000'), (0, 0x2A4050, 'ee780e0800000000')],
        )

    def test_official_blob_trailing_resident_code_is_accepted(self) -> None:
        blob = parse_patch_blob(OFFICIAL_BLOB)
        self.assertEqual(len(blob.entries), 2)
        self.assertEqual([entry.key for entry in blob.entries], [0x23, 0])
        self.assertEqual(len(blob.raw), 0x200)
        # Entries end at 0x34; the replacement code Capcom's hooks jump to follows.
        self.assertTrue(any(OFFICIAL_BLOB[0x34:]))

    def test_copy_entry_is_parsed(self) -> None:
        blob = (
            BLOB_MAGIC
            + struct.pack('<I', 1)
            + struct.pack('<III', 0x11, 0x12345678, 0x80000010)
            + struct.pack('<I', 0x00112233)
        )
        parsed = parse_patch_blob(blob)
        self.assertEqual(len(parsed.entries), 1)
        entry = parsed.entries[0]
        self.assertEqual(
            (entry.key, entry.destination, entry.length, entry.data, entry.source),
            (0x11, 0x12345678, 0x10, b'', 0x00112233),
        )

    def test_truncated_blob_is_rejected(self) -> None:
        for cut in (1, 4, 8):
            with self.assertRaises(ValueError, msg=cut):
                parse_patch_blob(CUSTOM_BLOB[:-cut])
        with self.assertRaises(ValueError):
            parse_patch_blob(CUSTOM_BLOB[:0x100])

    def test_bad_blob_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_patch_blob(b'not a patch' + bytes(100))
        with self.assertRaises(ValueError):
            parse_patch_blob(b'M-HUNTER' + struct.pack('<I', 0xFFFFFFFF) + bytes(20))

    def test_load_ram_patch_validates_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'good.bin'
            good.write_bytes(CUSTOM_BLOB)
            self.assertEqual(load_ram_patch(good, environment_key=RAM_PATCH_ENVIRONMENT_KEY), CUSTOM_BLOB)
            bad = Path(directory) / 'bad.bin'
            bad.write_bytes(b'M-HUNTER' + bytes(20))
            with self.assertRaises(ValueError) as caught:
                load_ram_patch(bad, environment_key=NA_RAM_PATCH_ENVIRONMENT_KEY)
            # The entrypoint log names the setting the operator must fix.
            self.assertIn(NA_RAM_PATCH_ENVIRONMENT_KEY, str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                load_ram_patch(bad, environment_key=RAM_PATCH_ENVIRONMENT_KEY)
            self.assertIn(RAM_PATCH_ENVIRONMENT_KEY, str(caught.exception))
            # The APP entrypoint catches ValueError, so an unusable path must
            # not surface as OSError.
            with self.assertRaises(ValueError):
                load_ram_patch(Path(directory) / 'missing.bin', environment_key=RAM_PATCH_ENVIRONMENT_KEY)
            with self.assertRaises(ValueError):
                load_ram_patch(Path(directory), environment_key=RAM_PATCH_ENVIRONMENT_KEY)

    def test_blob_that_does_not_fit_the_resident_buffer_is_rejected(self) -> None:
        # Entry data just large enough that the blob plus the longer (EU)
        # helper cannot fit, while the blob itself still parses.
        data = (
            BLOB_MAGIC
            + struct.pack('<I', 1)
            + struct.pack('<III', 0, 0, 65100)
            + bytes(65100)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'too-large.bin'
            path.write_bytes(data)
            with self.assertRaises(ValueError) as caught:
                load_ram_patch(path, environment_key=NA_RAM_PATCH_ENVIRONMENT_KEY)
        self.assertIn('resident buffer', str(caught.exception))


class RamPatchConfigTests(unittest.TestCase):
    """`OPENSNAP_MH_RAM_PATCH` reaches `AppServiceConfig.ram_patch` as a validated blob."""

    def setUp(self) -> None:
        self.config = default_app_config()

    def _read(self, value: str):
        with mock.patch.dict(
            os.environ,
            {
                RAM_PATCH_ENVIRONMENT_KEY: value,
                NA_RAM_PATCH_ENVIRONMENT_KEY: '',
            },
        ):
            return read_app_service_config(self.config, MONSTER_HUNTER_NA)

    def _read_na(self, value: str):
        with mock.patch.dict(
            os.environ,
            {
                RAM_PATCH_ENVIRONMENT_KEY: '',
                NA_RAM_PATCH_ENVIRONMENT_KEY: value,
            },
        ):
            return read_app_service_config(self.config, MONSTER_HUNTER_NA)

    def test_configured_blob_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'patch.bin'
            path.write_bytes(CUSTOM_BLOB)
            config = self._read(str(path))
            self.assertEqual(config.ram_patch, CUSTOM_BLOB)
            self.assertIsNone(config.ram_patch_na)
            config = self._read_na(str(path))
            self.assertEqual(config.ram_patch_na, CUSTOM_BLOB)
            self.assertIsNone(config.ram_patch)

    def test_unset_or_unreadable_blob(self) -> None:
        self.assertIsNone(self._read('').ram_patch)
        self.assertIsNone(self._read_na('').ram_patch_na)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                self._read(str(Path(directory) / 'missing.bin'))
            with self.assertRaises(ValueError) as caught:
                self._read_na(str(Path(directory) / 'missing.bin'))
            self.assertIn(NA_RAM_PATCH_ENVIRONMENT_KEY, str(caught.exception))
            # A malformed blob names the setting too, not just an unreadable path.
            bad = Path(directory) / 'bad.bin'
            bad.write_bytes(b'M-HUNTER' + bytes(20))
            with self.assertRaises(ValueError) as caught:
                self._read(str(bad))
            self.assertIn(RAM_PATCH_ENVIRONMENT_KEY, str(caught.exception))
            with self.assertRaises(ValueError) as caught:
                self._read_na(str(bad))
            self.assertIn(NA_RAM_PATCH_ENVIRONMENT_KEY, str(caught.exception))


class RamPatchPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = build_ram_patch(CUSTOM_BLOB, WORLDS, sequence=SEQUENCE)

    def test_offset_arithmetic_targets_the_resident_buffer(self) -> None:
        self.assertEqual(offset_to(UNCACHED_BLOB), 0x1F924100)
        self.assertEqual((WRITE_BASE + offset_to(UNCACHED_BLOB)) & 0xFFFFFFFF, UNCACHED_BLOB)
        self.assertEqual(offset_to(HANDLER_6502_REPLY), 0xFF8B0A0C)
        self.assertEqual((WRITE_BASE + handler_offset()) & 0xFFFFFFFF, HANDLER_6502_REPLY)
        self.assertEqual(self.plan.shellcode_address, 0x2039E8C0)

    def test_world_reply_keeps_the_real_list_and_adds_record_16(self) -> None:
        total, decoded = records(self.plan.prelude, SEQUENCE, start=0, count=WORLD_PAGE_SIZE)
        self.assertEqual(total, TOTAL_RECORDS)
        self.assertEqual(decoded[:2], [
            (b'world-a.example', b'World A', bytes(8), b'First world'),
            (b'world-b.example', b'World B', bytes(8), b'Second world'),
        ])
        self.assertTrue(all(entry == (b'', b'', bytes(8), b'') for entry in decoded[2:8]))
        total, decoded = records(
            self.plan.reply, SEQUENCE, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )
        # The final page carries the real list size; the write cursor, not the
        # total, is what reaches record 16.
        self.assertEqual(total, len(WORLDS))
        self.assertTrue(all(entry == (b'', b'', bytes(8), b'') for entry in decoded[:8]))

    def test_a_single_world_keeps_the_list_at_one_entry(self) -> None:
        plan = build_ram_patch(
            CUSTOM_BLOB, [('only.example', 'Only', '')], sequence=0
        )
        total, _ = records(plan.prelude, 0, start=0, count=WORLD_PAGE_SIZE)
        self.assertEqual(total, TOTAL_RECORDS)
        total, decoded = records(
            plan.reply, 0, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )
        self.assertEqual(total, 1)
        self.assertEqual(decoded[8][0][:2], bytes((0, DOWNLOAD_INDEX)))

    def test_record_16_primes_the_payload_upload(self) -> None:
        key, name, raw, description = records(
            self.plan.reply, SEQUENCE, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )[1][8]
        self.assertEqual(name, b'')
        self.assertEqual(len(description), EU_DESCRIPTION_FIELD_SIZE)
        self.assertEqual(description[:EU_DESCRIPTION_FRAME_HEADER], bytes(EU_DESCRIPTION_FRAME_HEADER))
        header = description[EU_DESCRIPTION_FRAME_HEADER:EU_DESCRIPTION_FRAME_HEADER + 8]
        self.assertEqual(header[:5], struct.pack('>HBH', len(self.plan.reply), 2, 0x6503))
        self.assertEqual(
            header[5], priming_sequence_byte(SEQUENCE, EU_DESCRIPTION_FRAME_HEADER + 5)
        )
        self.assertEqual(header[6:], b'\x00\xff')
        # The description also covers the receive body; it carries the page
        # prefix and pads the rest.
        prefix = self.plan.reply[:-(4 + EU_DESCRIPTION_FIELD_SIZE)]
        body = description[EU_DESCRIPTION_BODY:]
        expected = prefix[:len(body)] + bytes(len(body) - min(len(prefix), len(body)))
        self.assertEqual(body, expected)
        # The client keys the description checksum from the header byte inside
        # the field, so that byte must be the frame sequence.
        self.assertEqual(
            client_description(self.plan.reply, SEQUENCE, EU_DESCRIPTION_FRAME_HEADER + 5),
            description,
        )
        self.assertEqual(priming_key(key), (DOWNLOAD_INDEX, offset_to(UNCACHED_BLOB), DOWNLOAD_LIMIT))
        # array[1] is the end offset of the last byte the upload may write.
        self.assertEqual(
            struct.unpack_from('<I', raw, 1)[0], offset_to(UNCACHED_BLOB) + self.plan.payload_size
        )

    def test_payload_is_uploaded_in_754_byte_chunks(self) -> None:
        first = offset_to(UNCACHED_BLOB)
        positions = [0]
        while positions[-1] + CHUNK_SIZE < self.plan.payload_size:
            positions.append(positions[-1] + CHUNK_SIZE)
        uploads = [download(frame) for frame in self.plan.frames[:len(positions)]]
        self.assertEqual([index for index, _, _ in uploads], [DOWNLOAD_INDEX] * len(positions))
        self.assertEqual([offset for _, offset, _ in uploads], [first + position for position in positions])
        data = b''.join(chunk for _, _, chunk in uploads)
        self.assertEqual(data[:len(CUSTOM_BLOB)], CUSTOM_BLOB)
        self.assertEqual(len(data), self.plan.payload_size)
        self.assertEqual(
            [len(chunk) for _, _, chunk in uploads],
            [CHUNK_SIZE] * (len(positions) - 1) + [self.plan.payload_size - positions[-1]],
        )

    def test_handler_frame_arms_the_6502_reply(self) -> None:
        index, offset, data = download(self.plan.frames[-2])
        self.assertEqual((index, offset), (DOWNLOAD_INDEX, handler_offset()))
        self.assertEqual(data, struct.pack('<I', self.plan.shellcode_address))

    def test_page_b_primes_the_handler_write_after_a_prelude(self) -> None:
        prelude, page_b = self.plan.frames[-4], self.plan.frames[-3]
        self.assertEqual(frame_command(prelude), 0x6503)
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(prelude)), (16, 0, 8))
        self.assertEqual(frame_command(page_b), 0x6503)
        self.assertEqual(frame_sequence(page_b), 0)
        key, _, raw, _ = records(
            frame_payload(page_b), 0, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )[1][8]
        self.assertEqual(priming_key(key), (DOWNLOAD_INDEX, handler_offset(), DOWNLOAD_LIMIT))
        self.assertEqual(struct.unpack_from('<I', raw, 1)[0], handler_offset() + 4)

    def test_the_burst_ends_with_a_pushed_6502_reply(self) -> None:
        reply = self.plan.frames[-1]
        self.assertEqual((frame_command(reply), frame_sequence(reply)), (0x6502, 0))
        self.assertEqual(frame_payload(reply), WORLD_STATUS_REPLY)

    def test_world_count_limits(self) -> None:
        with self.assertRaises(ValueError):
            build_ram_patch(CUSTOM_BLOB, [], sequence=0)
        with self.assertRaises(ValueError):
            build_ram_patch(CUSTOM_BLOB, [('h', f'W{index}', '') for index in range(17)], sequence=0)
        # Sixteen Worlds fill records 0..15; the final page ends with record 16.
        plan = build_ram_patch(CUSTOM_BLOB, [('h', f'W{index}', '') for index in range(16)], sequence=0)
        total, decoded = records(plan.prelude, 0, start=0, count=WORLD_PAGE_SIZE)
        self.assertEqual(total, TOTAL_RECORDS)
        total, decoded = records(
            plan.reply, 0, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )
        self.assertEqual(total, TOTAL_RECORDS)
        self.assertEqual(decoded[7][1], b'W15')


class ShellcodeTests(unittest.TestCase):
    def test_shellcode_is_stable(self) -> None:
        code = build_shellcode(len(CUSTOM_BLOB))
        self.assertEqual(len(code), 504)
        self.assertEqual(
            hashlib.sha256(code).hexdigest(),
            '513c3de1c97f8a81b19b6856093e30a590eadf0215092cbed447e0dd85f5b853',
        )
        words = struct.unpack(f'<{len(code) // 4}I', code)
        # li t1, 0x2039e300 (uncached source) and li t2, 0x0039e300 (cached copy).
        self.assertEqual(words[3:5], (0x3C092039, 0x3529E300))
        self.assertEqual(words[5:7], (0x3C0A0039, 0x354AE300))
        # The tail call restores the real 6502 handler (`jr` plus its delay slot).
        self.assertEqual(words[-4:-1], (0x3C08002A, 0x35082420, 0x01000008))

    def test_empty_blob_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_shellcode(0)


def na_description(frame: bytes) -> bytes:
    """Record 16's description of one NA 6503 page."""

    return records(
        frame_payload(frame),
        frame_sequence(frame),
        start=FINAL_PAGE_START,
        count=FINAL_PAGE_COUNT,
    )[1][8][3]


class NaRamPatchPlanTests(unittest.TestCase):
    """The NA burst: 6002 suffix writes, three priming pages and the guard."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = build_na_ram_patch(NA_BLOB, WORLDS, sequence=SEQUENCE)
        cls.uploads = (cls.plan.payload_size + NA_CHUNK_SIZE - 1) // NA_CHUNK_SIZE
        cls.upload_base = (NA_UNCACHED_BLOB + cls.plan.payload_size - NA_WINDOW) & 0xFFFFFFFF

    def test_payload_is_uploaded_in_512_byte_suffix_writes(self) -> None:
        # Frame 0 clears the 6002 guard through 6204; the uploads follow it.
        guard = self.plan.frames[0]
        self.assertEqual(frame_command(guard), 0x6204)
        patches = [na_patch(frame) for frame in self.plan.frames[1:1 + self.uploads]]
        positions = list(range(0, self.plan.payload_size, NA_CHUNK_SIZE))
        self.assertEqual([name for name, _, _ in patches], [NA_NAME] * self.uploads)
        self.assertEqual(
            [offset for _, offset, _ in patches],
            [NA_WINDOW - self.plan.payload_size + position for position in positions],
        )
        data = b''.join(chunk for _, _, chunk in patches)
        self.assertEqual(len(data), self.plan.payload_size)
        self.assertEqual(data[:len(NA_BLOB)], NA_BLOB)
        shellcode_offset = (len(NA_BLOB) + 15) & ~15
        self.assertEqual(data[shellcode_offset:], build_na_shellcode(len(NA_BLOB)))
        # Every write's base + window ends where the payload ends; the handler
        # zero-fills that suffix, so each chunk only clears bytes not yet sent.
        self.assertEqual(
            (self.upload_base + NA_WINDOW) & 0xFFFFFFFF,
            (NA_UNCACHED_BLOB + self.plan.payload_size) & 0xFFFFFFFF,
        )
        for position, (_, offset, chunk) in zip(positions, patches):
            self.assertEqual(
                (self.upload_base + offset) & 0xFFFFFFFF,
                (NA_UNCACHED_BLOB + position) & 0xFFFFFFFF,
            )
            self.assertLessEqual(offset + len(chunk), NA_WINDOW)
        self.assertEqual(patches[-1][1] + len(patches[-1][2]), NA_WINDOW)

    def test_page_a_primes_the_6002_globals_in_record_16(self) -> None:
        total, decoded = records(self.plan.prelude, SEQUENCE, start=0, count=WORLD_PAGE_SIZE)
        self.assertEqual(total, TOTAL_RECORDS)
        self.assertEqual(decoded[0][:3], (b'world-a.example', b'World A', bytes(8)))
        total, decoded = records(
            self.plan.reply, SEQUENCE, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )
        self.assertEqual(total, len(WORLDS))
        # The key field is also the 1002 login block (fixed pointers from
        # `0x00247fc4`); restoring it keeps the next connection's 1002 valid.
        self.assertEqual(decoded[8][0], NA_LOGIN_BLOCK)
        description = decoded[8][3]
        self.assertEqual(len(description), NA_DESCRIPTION_FIELD_SIZE)
        # The primed globals end at the write base's four bytes
        # (`NA_DESCRIPTION_LENGTH`); the rest up to the frame header is padding.
        self.assertEqual(
            description[NA_DESCRIPTION_LENGTH:NA_DESCRIPTION_FRAME_HEADER],
            bytes(NA_DESCRIPTION_FRAME_HEADER - NA_DESCRIPTION_LENGTH),
        )
        self.assertEqual(description[:NA_DESCRIPTION_TABLE], bytes(NA_DESCRIPTION_TABLE))
        guard_offset = (NA_GUARD - NA_DOWNLOAD_BASE) & 0xFFFFFFFF
        self.assertEqual(
            struct.unpack_from('<I', description, NA_DESCRIPTION_TABLE + 4)[0],
            (guard_offset + 1) & 0xFFFFFFFF,
        )
        # Record 16's name field primes 6204's expected index and offset.
        name = decoded[8][1]
        self.assertEqual(len(name), 15)
        self.assertEqual(name[NA_DOWNLOAD_INDEX_OFFSET], NA_DOWNLOAD_INDEX)
        self.assertEqual(
            struct.unpack_from('<I', name, NA_DOWNLOAD_OFFSET_OFFSET)[0], guard_offset
        )
        self.assertEqual(
            description[NA_DESCRIPTION_NAME - NA_RECORD_DESCRIPTION:
                        NA_DESCRIPTION_VERSION - NA_RECORD_DESCRIPTION],
            NA_NAME + bytes(6),
        )
        self.assertEqual(
            description[NA_DESCRIPTION_VERSION - NA_RECORD_DESCRIPTION:
                        NA_DESCRIPTION_SIZE - NA_RECORD_DESCRIPTION],
            b'1002' + bytes(7),
        )
        self.assertEqual(
            struct.unpack_from('<I', description, NA_DESCRIPTION_SIZE - NA_RECORD_DESCRIPTION)[0],
            self.plan.payload_size,
        )
        self.assertEqual(
            struct.unpack_from('<I', description, NA_WRITE_BASE - NA_RECORD_DESCRIPTION)[0],
            self.upload_base,
        )
        header = description[NA_DESCRIPTION_FRAME_HEADER:NA_DESCRIPTION_FRAME_HEADER + 8]
        self.assertEqual(header[:5], struct.pack('>HBH', len(self.plan.reply), 2, 0x6503))
        self.assertEqual(
            header[5], priming_sequence_byte(SEQUENCE, NA_DESCRIPTION_FRAME_HEADER + 5)
        )
        self.assertEqual(header[6:], b'\x00\xff')
        self.assertEqual(
            description[NA_DESCRIPTION_BODY:],
            self.plan.reply[:NA_DESCRIPTION_BODY_SIZE],
        )
        self.assertEqual(
            client_description(self.plan.reply, SEQUENCE, NA_DESCRIPTION_FRAME_HEADER + 5),
            description,
        )

    def test_handler_and_guard_writes_use_window_suffixes(self) -> None:
        # [guard 6204][uploads][prelude][handler page][handler 6002]
        # [guard page][guard 6002][trigger page][pushed 6502]
        first = 1 + self.uploads
        page_prelude, page_b, handler, page_b2, guard, page_c, status = self.plan.frames[first:]
        self.assertEqual(
            [frame_command(frame) for frame in (page_prelude, page_b, page_b2, page_c)],
            [0x6503] * 4,
        )
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(page_prelude)), (16, 0, 8))
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(page_b)), (len(WORLDS), 8, 9))

        handler_base = (NA_HANDLER_6502_REPLY - (NA_WINDOW - 4)) & 0xFFFFFFFF
        self.assertEqual(
            struct.unpack_from('<I', na_description(page_b), 89)[0], handler_base
        )
        name, offset, data = na_patch(handler)
        self.assertEqual((name, offset), (NA_NAME, 0xFFFC))
        self.assertEqual(data, struct.pack('<I', self.plan.shellcode_address))
        self.assertEqual((handler_base + offset) & 0xFFFFFFFF, NA_HANDLER_6502_REPLY)

        guard_base = (NA_GUARD - (NA_WINDOW - 1)) & 0xFFFFFFFF
        for page in (page_b2, page_c):
            self.assertEqual(struct.unpack_from('<I', na_description(page), 89)[0], guard_base)
        name, offset, data = na_patch(guard)
        self.assertEqual((name, offset, data), (NA_NAME, 0xFFFF, b'\x02'))
        self.assertEqual((guard_base + offset) & 0xFFFFFFFF, NA_GUARD)

        self.assertEqual((frame_command(status), frame_sequence(status)), (0x6502, 0))
        self.assertEqual(frame_payload(status), WORLD_STATUS_REPLY)

    def test_plan_ignores_the_requests_the_burst_causes(self) -> None:
        self.assertEqual(self.plan.variant, 'na')
        self.assertEqual(self.plan.ignore_6502, 2)
        self.assertEqual(self.plan.ignore_6503, 1)
        self.assertEqual(self.plan.ignore_6211, 1)
        self.assertEqual(self.plan.ignore_6403, 2)
        self.assertEqual(self.plan.ignore_6002, self.uploads + 2)
        self.assertEqual(self.plan.shellcode_address, NA_UNCACHED_BLOB + ((len(NA_BLOB) + 15) & ~15))

    def test_world_count_limits(self) -> None:
        with self.assertRaises(ValueError):
            build_na_ram_patch(NA_BLOB, [], sequence=0)
        with self.assertRaises(ValueError):
            build_na_ram_patch(NA_BLOB, [('h', f'W{index}', '') for index in range(17)], sequence=0)
        plan = build_na_ram_patch(NA_BLOB, [('h', f'W{index}', '') for index in range(16)], sequence=0)
        total, decoded = records(plan.prelude, 0, start=0, count=WORLD_PAGE_SIZE)
        self.assertEqual(total, TOTAL_RECORDS)
        total, decoded = records(
            plan.reply, 0, start=FINAL_PAGE_START, count=FINAL_PAGE_COUNT
        )
        self.assertEqual(total, TOTAL_RECORDS)
        self.assertEqual(decoded[7][1], b'W15')


class RamPatchPageLimitTests(unittest.TestCase):
    """The client's 768-byte APP body caps the priming page, for both builds."""

    def test_short_pages_fit(self) -> None:
        for plan in (
            build_ram_patch(CUSTOM_BLOB, WORLDS, sequence=0),
            build_na_ram_patch(NA_BLOB, WORLDS, sequence=0),
        ):
            self.assertLessEqual(len(plan.prelude), APP_BODY_LIMIT)
            self.assertLessEqual(len(plan.reply), APP_BODY_LIMIT)
            for frame in plan.frames:
                self.assertLessEqual(len(frame_payload(frame)), APP_BODY_LIMIT)

    def test_oversized_world_page_is_rejected(self) -> None:
        worlds = [('h', 'World', 'x' * 700)]
        for build, blob in ((build_ram_patch, CUSTOM_BLOB), (build_na_ram_patch, NA_BLOB)):
            with self.assertRaises(RamPatchPageTooLarge, msg=build.__name__):
                build(blob, worlds, sequence=0)


class NaShellcodeTests(unittest.TestCase):
    def test_shellcode_is_stable(self) -> None:
        code = build_na_shellcode(len(NA_BLOB))
        self.assertEqual(len(code), 460)
        self.assertEqual(
            hashlib.sha256(code).hexdigest(),
            '348807f9cd90f712230a6338f23cb1a7a65341a9876589f7cb47802311d77179',
        )
        words = struct.unpack(f'<{len(code) // 4}I', code)
        # li t1, 0x203d9980 (uncached source) and li t2, 0x003d9980 (cached copy).
        self.assertEqual(words[3:5], (0x3C09203D, 0x35299980))
        self.assertEqual(words[5:7], (0x3C0A003D, 0x354A9980))
        # The applier, overlay lookup, both flushes and the handler restore are
        # the NA addresses, not EU ones.
        for address in (NA_APPLIER, NA_OVERLAY_ID, NA_FLUSH_CACHE, NA_HANDLER_6502, NA_HANDLER_6502_REPLY):
            pair = (0x3C080000 | (address >> 16), 0x35080000 | (address & 0xFFFF))
            self.assertIn(pair, set(zip(words, words[1:])), hex(address))
        # The tail restores the APP mode byte (also the 6002 guard) to 1 so the
        # 6510 reply handler ends the connection with 1004 instead of code -1 (D901):
        # `li t0, 0x399654; addiu t1, zero, 1; sb t1, 0(t0)`.
        self.assertIn(
            (0x3C080039, 0x35089654, 0x24090001, 0xA1090000),
            set(zip(words, words[1:], words[2:], words[3:])),
        )
        # The tail call restores the real 6502 handler (`jr` plus its delay slot).
        self.assertEqual(words[-4:-1], (0x3C08002B, 0x35082270, 0x01000008))

    def test_empty_blob_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_na_shellcode(0)
