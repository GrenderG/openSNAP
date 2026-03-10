"""Auto Modellista AM-USA-GAME-RULE serializer tests."""

import unittest

from opensnap_web.games.automodellista import (
    AM_RULE_MENU_METADATA,
    build_am_rule_csv_rows,
    build_am_rule_page,
    serialize_am_performance_row,
    serialize_am_rule_row,
)
from opensnap_web.games.automodellista_beta1 import (
    AM_BETA1_RULE_MENU_METADATA,
    build_ambeta1_rule_csv_rows,
    build_ambeta1_rule_page,
    serialize_ambeta1_rule_row,
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
                '00000a0000080f000100000000000000000000280000000000000000',
                '00000a0000080f000100000000000000000000280000000000000000',
                '00000a0000080f000100000000000000000000280000000000000000',
                '00000a0000030f000100000000000000000000280000000000000100',
                '0000000000000f000100000000000000000000110000000000000000',
                performance.hex(),
                '00',
            ),
        )

    def test_rule_page_preserves_marker_and_profile_comments(self) -> None:
        page = build_am_rule_page()
        self.assertIn('<!--AM-USA-GAME-RULE-->', page)
        self.assertIn('<!-- 0 Mountain -->', page)
        self.assertIn('<!-- 3 Event / Clubmeeting runtime -->', page)
        self.assertIn('<!-- 4 Clubmeeting active runtime -->', page)
        self.assertIn('<!-- 5 Performance -->', page)
        self.assertIn('"00000a0000030f000100000000000000000000280000000000000100",', page)
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

    def test_serializer_packs_max_people_default_count_alias_into_byte_19(self) -> None:
        row = serialize_am_rule_row(
            template='normal',
            field_overrides={'max_people_default_count': 0x08},
        )
        self.assertEqual(row[19], 0x28)

    def test_serializer_emits_stock_finish_grace_seconds_at_byte_6(self) -> None:
        row = serialize_am_rule_row(template='normal')
        self.assertEqual(row[6], 0x0F)

    def test_clubmeeting_active_row_enables_chat_gate_at_byte_8(self) -> None:
        rows = build_am_rule_csv_rows()
        active_row = bytes.fromhex(rows[4])
        self.assertEqual(active_row[8], 0x01)

    def test_serializer_rejects_out_of_range_max_people_default_count(self) -> None:
        with self.assertRaises(ValueError):
            serialize_am_rule_row(
                template='normal',
                field_overrides={'max_people_default_count': 0x09},
            )

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

    def test_release_and_beta1_menu_metadata_share_common_schema_keys(self) -> None:
        expected_keys = {
            'rule_titles',
            'rule_names',
            'course_types',
            'choice_count_matrix',
            'default_index_matrix',
            'choice_counts',
            'choice_options_by_course_type',
            'choice_counts_by_course_type',
            'default_indexes',
            'default_indexes_by_course_type',
            'courses_by_type',
        }
        self.assertTrue(expected_keys.issubset(set(AM_RULE_MENU_METADATA)))
        self.assertTrue(expected_keys.issubset(set(AM_BETA1_RULE_MENU_METADATA)))

    def test_release_and_beta1_menu_metadata_matrices_match_declared_dimensions(self) -> None:
        for metadata in (AM_RULE_MENU_METADATA, AM_BETA1_RULE_MENU_METADATA):
            rule_names = metadata['rule_names']
            course_types = metadata['course_types']
            choice_matrix = metadata['choice_count_matrix']
            default_matrix = metadata['default_index_matrix']
            self.assertEqual(len(choice_matrix), len(rule_names))
            self.assertEqual(len(default_matrix), len(rule_names))
            for row in choice_matrix:
                self.assertEqual(len(row), len(course_types))
            for row in default_matrix:
                self.assertEqual(len(row), len(course_types))


class AutoModellistaBeta1RuleSerializationTests(unittest.TestCase):
    """Validate Beta1 AM-USA-GAME-RULE 3x26 serializer behavior."""

    def test_beta1_default_profiles_match_3x26_contract(self) -> None:
        self.assertEqual(
            build_ambeta1_rule_csv_rows(),
            (
                '00000a0000080f00010000000000000000280000000000000000',
                '00000a0000080f00010000000000000000280000000000000000',
                '00000a0000080f00010000000000000000280000000000000000',
            ),
        )

    def test_beta1_rule_page_preserves_marker_and_profile_comments(self) -> None:
        page = build_ambeta1_rule_page()
        self.assertIn('<!--AM-USA-GAME-RULE-->', page)
        self.assertIn('<!-- 0 Mountain -->', page)
        self.assertIn('<!-- 2 Circuit -->', page)
        self.assertIn('"00000a0000080f00010000000000000000280000000000000000"', page)
        self.assertTrue(page.endswith('</html>\n'))

    def test_beta1_serializer_packs_player_defaults_at_offset_17(self) -> None:
        row = serialize_ambeta1_rule_row(
            template='normal',
            field_overrides={'needed_players_default': 0x03, 'max_people_default': 0x06},
        )
        self.assertEqual(len(row), 26)
        self.assertEqual(row[17], 0x36)

    def test_beta1_serializer_packs_max_people_default_count_alias_at_offset_17(self) -> None:
        row = serialize_ambeta1_rule_row(
            template='normal',
            field_overrides={'max_people_default_count': 0x08},
        )
        self.assertEqual(row[17], 0x28)

    def test_beta1_serializer_emits_stock_finish_grace_seconds_at_byte_6(self) -> None:
        row = serialize_ambeta1_rule_row(template='normal')
        self.assertEqual(row[6], 0x0F)


if __name__ == '__main__':
    unittest.main()
