"""Auto Modellista AM-USA-GAME-RULE serializer tests."""

import unittest

from opensnap_web.games.automodellista import (
    build_am_rule_csv_rows,
    build_am_rule_page,
    serialize_am_performance_row,
    serialize_am_rule_row,
)


class AutoModellistaRuleSerializationTests(unittest.TestCase):
    """Validate structured AM rule profiles serialize to expected overlay rows."""

    def test_default_profiles_match_mode9_parser_contract(self) -> None:
        performance = bytearray(64)
        performance[2] = 0x0A
        performance[5] = 0x08
        performance[8] = 0x01
        performance[19] = 0x22
        self.assertEqual(
            build_am_rule_csv_rows(),
            (
                '00000a00000800000100000000000000000000240000000000000000',
                '00000a00000800000100000000000000000000240000000000000000',
                '00000a00000800000100000000000000000000240000000000000000',
                '00000a00000800000100000000000000000000440000000000000100',
                '00000000000000000000000000000000000000110000000000000000',
                performance.hex(),
                '00',
            ),
        )

    def test_rule_page_preserves_marker_and_profile_comments(self) -> None:
        page = build_am_rule_page()
        self.assertIn('<!--AM-USA-GAME-RULE-->', page)
        self.assertIn('<!-- 0 Mountain -->', page)
        self.assertIn('<!-- 4 Clubmeeting -->', page)
        self.assertIn('<!-- 5 Performance -->', page)
        self.assertIn('"00000a00000800000100000000000000000000440000000000000100",', page)
        self.assertTrue(page.endswith('</html>\n'))

    def test_serializer_supports_template_and_explicit_byte_overrides(self) -> None:
        row = serialize_am_rule_row(template='blank', byte_overrides={19: 0x33, 26: 0x01})
        self.assertEqual(
            row.hex(),
            '00000000000000000000000000000000000000330000000000000100',
        )

    def test_serializer_packs_semantic_player_defaults_into_byte_19(self) -> None:
        row = serialize_am_rule_row(
            template='blank',
            field_overrides={'needed_players_default': 0x03, 'max_people_default': 0x06},
        )
        self.assertEqual(row[19], 0x36)

    def test_performance_serializer_supports_explicit_byte_overrides(self) -> None:
        row = serialize_am_performance_row(byte_overrides={0: 0x01, 63: 0xFF})
        self.assertEqual(len(row), 64)
        self.assertEqual(row[0], 0x01)
        self.assertEqual(row[63], 0xFF)

    def test_performance_serializer_supports_semantic_field_overrides(self) -> None:
        row = serialize_am_performance_row(
            field_overrides={'course_mode_seed': 0x0A, 'needed_players_default': 0x02, 'max_people_default': 0x03},
        )
        self.assertEqual(row[2], 0x0A)
        self.assertEqual(row[19], 0x23)

    def test_performance_serializer_rejects_out_of_range_offsets(self) -> None:
        with self.assertRaises(ValueError):
            serialize_am_performance_row(byte_overrides={64: 0x01})

    def test_serializer_rejects_unknown_template(self) -> None:
        with self.assertRaises(ValueError):
            serialize_am_rule_row(template='missing')


if __name__ == '__main__':
    unittest.main()
