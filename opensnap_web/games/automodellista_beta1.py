"""Auto Modellista Beta1 web routes and AM-USA-GAME-RULE serializer."""

from collections.abc import Mapping

from opensnap_web.games.automodellista import (
    AM_RULE_COURSE_MODE_SEED_STOCK,
    AM_RULE_DEFAULT_INDEX_STANDARD_MAX_PEOPLE,
    AM_RULE_DEFAULT_INDEX_STANDARD_NEEDED_PLAYERS,
    AM_RULE_EDIT_MASK_STOCK_EDITABLE,
    AM_RULE_LAP_SEED_STOCK,
    AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
    AutoModellistaWebModule,
    _apply_byte_overrides,
    _coerce_max_people_default_count,
    _coerce_rule_byte,
    _coerce_rule_nibble,
    _pack_players_packed,
    _required_config_sequence,
    _required_rule_profile_int,
    _required_rule_profile_str,
    _rule_profile_mapping,
)

# ---------------------------------------------------------------------------
# Beta1 AM-USA-GAME-RULE contract.
#
# Binary anchors:
# - `SLUS_204.98` `lbc_in_lobby_00` (0x002c8b70):
#   - reads packed defaults from `BsGameRule + 17` (high/low nibbles).
# - `SLUS_204.98` `To_ReadyBattle` (0x002cc140):
#   - copies 26 bytes from selected `BsGameRule` row.
# - `ambeta1_bin/browser.bin` `get_crs_shadow_data` mode-9 case (0x008c5c70):
#   - `memset(0x52abe0, 0, 111)` => 3 * 37-byte rows,
#   - loops 3 times reading numeric rows with length 26 and destination stride 37.
#
# Unlike retail `SLUS_206.42`:
# - no 64-byte performance row parse in mode-9,
# - no 5x28 rule-row contract.
# ---------------------------------------------------------------------------
AM_BETA1_RULE_PROFILE_ROW_COUNT = 3
AM_BETA1_RULE_ROW_SIZE = 26

# `BsGameRule` known offsets confirmed from consumers above.
AM_BETA1_RULE_ROW_FIELDS = {
    'course_mode_seed': {
        'offset': 2,
        'meaning': 'Patched in `To_ReadyBattle` from current course selection mapping.',
    },
    'lap_seed': {
        'offset': 5,
        'meaning': 'Patched in `To_ReadyBattle` from lobby rule state (+1).',
    },
    'edit_mask': {
        'offset': 8,
        'meaning': 'Rule behavior/editability mask (same seed used by stock rows).',
    },
    'players_packed': {
        'offset': 17,
        'meaning': (
            'Packed defaults: high nibble=`Needed players`, '
            'low nibble=`No. of People`.'
        ),
    },
}

AM_BETA1_RULE_OFFSET_COURSE_MODE_SEED = int(AM_BETA1_RULE_ROW_FIELDS['course_mode_seed']['offset'])
AM_BETA1_RULE_OFFSET_LAP_SEED = int(AM_BETA1_RULE_ROW_FIELDS['lap_seed']['offset'])
AM_BETA1_RULE_OFFSET_EDIT_MASK = int(AM_BETA1_RULE_ROW_FIELDS['edit_mask']['offset'])
AM_BETA1_RULE_OFFSET_PLAYERS_PACKED = int(AM_BETA1_RULE_ROW_FIELDS['players_packed']['offset'])

AM_BETA1_RULE_SCALAR_FIELD_OFFSETS = {
    'course_mode_seed': AM_BETA1_RULE_OFFSET_COURSE_MODE_SEED,
    'lap_seed': AM_BETA1_RULE_OFFSET_LAP_SEED,
    'edit_mask': AM_BETA1_RULE_OFFSET_EDIT_MASK,
}

AM_BETA1_RULE_PACKED_FIELD_PLAYERS = 'players_packed'
AM_BETA1_RULE_PACKED_FIELD_NEEDED_PLAYERS_DEFAULT = 'needed_players_default'
AM_BETA1_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT = 'max_people_default'
AM_BETA1_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT_COUNT = 'max_people_default_count'

AM_BETA1_RULE_KNOWN_OFFSETS = frozenset(
    (
        AM_BETA1_RULE_OFFSET_COURSE_MODE_SEED,
        AM_BETA1_RULE_OFFSET_LAP_SEED,
        AM_BETA1_RULE_OFFSET_EDIT_MASK,
        AM_BETA1_RULE_OFFSET_PLAYERS_PACKED,
    )
)
AM_BETA1_RULE_UNKNOWN_OFFSETS = tuple(
    offset for offset in range(AM_BETA1_RULE_ROW_SIZE) if offset not in AM_BETA1_RULE_KNOWN_OFFSETS
)

# Rule title/count/default tables from `SLUS_204.98`.
#
# `rule_title_tbl` (0x003d5a40) has 5 entries in file order:
# - Course Settings
# - Needed players
# - No. of People
# - Lap
# - Boost
#
# `lbc_in_lobby_16` sets active count to 4 and loads the first 4 entries.
AM_BETA1_RULE_TITLES = (
    'Course Settings',
    'Needed players',
    'No. of People',
    'Lap',
)

AM_BETA1_COURSE_TYPES = ('mountain', 'city', 'circuit')
AM_BETA1_RULE_NAMES = ('course_settings', 'needed_players', 'no_of_people', 'lap')

# `rule_choice_suu_tbl` raw bytes at `0x00425028` (4 rules x 3 course types):
# 01 01 01  01 01 05  07 07 01  02 02 00
AM_BETA1_RULE_CHOICE_COUNT_MATRIX = (
    (1, 1, 1),
    (1, 1, 5),
    (7, 7, 1),
    (2, 2, 0),
)

# `rule_default_tbl` raw bytes at `0x00425038` (4 rules x 3 course types):
# 00 00 00  00 00 00  00 00 00  00 01 01
AM_BETA1_RULE_DEFAULT_INDEX_MATRIX = (
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 1, 1),
)

AM_BETA1_RULE_CHOICE_COUNTS = {
    rule_name: {
        course_type: AM_BETA1_RULE_CHOICE_COUNT_MATRIX[rule_index][course_type_index]
        for course_type_index, course_type in enumerate(AM_BETA1_COURSE_TYPES)
    }
    for rule_index, rule_name in enumerate(AM_BETA1_RULE_NAMES)
}

AM_BETA1_RULE_DEFAULT_INDEXES = {
    rule_name: {
        course_type: AM_BETA1_RULE_DEFAULT_INDEX_MATRIX[rule_index][course_type_index]
        for course_type_index, course_type in enumerate(AM_BETA1_COURSE_TYPES)
    }
    for rule_index, rule_name in enumerate(AM_BETA1_RULE_NAMES)
}

# Some Beta1 rule labels are still unresolved from text tables in this pass.
# Keep stable placeholder option ids where labels are not yet confirmed.
# These tuples are symbolic option-slot ids only (not on-wire values); they preserve
# menu cardinality and stable ordering for unresolved label tables.
AM_BETA1_RULE_OPTION_INDEXES_EMPTY: tuple[str, ...] = ()
AM_BETA1_RULE_OPTION_INDEXES_SINGLE = ('choice_idx_0',)
AM_BETA1_RULE_OPTION_INDEXES_PAIR = ('choice_idx_0', 'choice_idx_1')
AM_BETA1_RULE_OPTION_INDEXES_QUINTUPLE = (
    'choice_idx_0',
    'choice_idx_1',
    'choice_idx_2',
    'choice_idx_3',
    'choice_idx_4',
)
AM_BETA1_RULE_OPTION_INDEXES_SEPTUPLE = (
    'choice_idx_0',
    'choice_idx_1',
    'choice_idx_2',
    'choice_idx_3',
    'choice_idx_4',
    'choice_idx_5',
    'choice_idx_6',
)

AM_BETA1_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE = {
    'mountain': {
        'course_settings': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'needed_players': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'no_of_people': AM_BETA1_RULE_OPTION_INDEXES_SEPTUPLE,
        'lap': AM_BETA1_RULE_OPTION_INDEXES_PAIR,
    },
    'city': {
        'course_settings': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'needed_players': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'no_of_people': AM_BETA1_RULE_OPTION_INDEXES_SEPTUPLE,
        'lap': AM_BETA1_RULE_OPTION_INDEXES_PAIR,
    },
    'circuit': {
        'course_settings': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'needed_players': AM_BETA1_RULE_OPTION_INDEXES_QUINTUPLE,
        'no_of_people': AM_BETA1_RULE_OPTION_INDEXES_SINGLE,
        'lap': AM_BETA1_RULE_OPTION_INDEXES_EMPTY,
    },
}

AM_BETA1_RULE_CHOICE_COUNTS_BY_COURSE_TYPE = {
    course_type: {rule_name: AM_BETA1_RULE_CHOICE_COUNTS[rule_name][course_type] for rule_name in AM_BETA1_RULE_NAMES}
    for course_type in AM_BETA1_COURSE_TYPES
}

# Validate that placeholder option catalogs keep matching the binary row counts.
for _course_type, _rule_options in AM_BETA1_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE.items():
    for _rule_name, _options in _rule_options.items():
        if len(_options) != AM_BETA1_RULE_CHOICE_COUNTS_BY_COURSE_TYPE[_course_type][_rule_name]:
            raise ValueError(
                f'AM Beta1 rule choice option length mismatch: '
                f'{_course_type}.{_rule_name} has {len(_options)} options, '
                f'expected {AM_BETA1_RULE_CHOICE_COUNTS_BY_COURSE_TYPE[_course_type][_rule_name]}'
            )

AM_BETA1_RULE_DEFAULT_INDEXES_BY_COURSE_TYPE = {
    course_type: {rule_name: AM_BETA1_RULE_DEFAULT_INDEXES[rule_name][course_type] for rule_name in AM_BETA1_RULE_NAMES}
    for course_type in AM_BETA1_COURSE_TYPES
}

AM_BETA1_COURSES_BY_TYPE = {
    course_type: AM_BETA1_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE[course_type]['course_settings']
    for course_type in AM_BETA1_COURSE_TYPES
}

AM_BETA1_RULE_MENU_METADATA = {
    'rule_titles': AM_BETA1_RULE_TITLES,
    'rule_names': AM_BETA1_RULE_NAMES,
    'course_types': AM_BETA1_COURSE_TYPES,
    'choice_count_matrix': AM_BETA1_RULE_CHOICE_COUNT_MATRIX,
    'default_index_matrix': AM_BETA1_RULE_DEFAULT_INDEX_MATRIX,
    'choice_counts': AM_BETA1_RULE_CHOICE_COUNTS,
    'choice_options_by_course_type': AM_BETA1_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE,
    'choice_counts_by_course_type': AM_BETA1_RULE_CHOICE_COUNTS_BY_COURSE_TYPE,
    'default_indexes': AM_BETA1_RULE_DEFAULT_INDEXES,
    'default_indexes_by_course_type': AM_BETA1_RULE_DEFAULT_INDEXES_BY_COURSE_TYPE,
    'courses_by_type': AM_BETA1_COURSES_BY_TYPE,
}

AM_BETA1_RULE_TEMPLATE_NAME_NORMAL = 'normal'

# Seed row used for all three Beta1 profiles by default.
#
# Known semantic defaults:
# - +2  course_mode_seed = 0x0A
# - +5  lap_seed         = 0x08
# - +8  edit_mask        = 0x01
# - +17 players_packed   = 0x24 (needed=2, max_people=4)
#
# Important Beta1 editability note (`SLUS_204.98`):
# - `lbc_in_lobby_16` (`0x002ca780..0x002ca7cc`) unlocks editable
#   `No. of People` only when packed defaults are `(needed=2, max_people=8)`;
# - that means byte `+17` must be `0x28`;
# - `SLUS_204.98` still hard-locks course settings, needed players, and lap in
#   code (`sb zero` to their editable flags), so this ruleset can only align the
#   `No. of People` behavior with release-style standard profiles.
AM_BETA1_RULE_TEMPLATE_NORMAL = {
    'course_mode_seed': AM_RULE_COURSE_MODE_SEED_STOCK,
    'lap_seed': AM_RULE_LAP_SEED_STOCK,
    'edit_mask': AM_RULE_EDIT_MASK_STOCK_EDITABLE,
    'needed_players_default': AM_RULE_DEFAULT_INDEX_STANDARD_NEEDED_PLAYERS,
    'max_people_default': AM_RULE_DEFAULT_INDEX_STANDARD_MAX_PEOPLE,
}

AM_BETA1_RULE_ROW_TEMPLATES = {
    AM_BETA1_RULE_TEMPLATE_NAME_NORMAL: AM_BETA1_RULE_TEMPLATE_NORMAL,
}

AM_BETA1_RULE_PROFILE_INDEX_MOUNTAIN = 0
AM_BETA1_RULE_PROFILE_INDEX_CITY = 1
AM_BETA1_RULE_PROFILE_INDEX_CIRCUIT = 2

AM_BETA1_GAME_RULE_CONFIG = {
    'rule_profiles': (
        {
            'index': AM_BETA1_RULE_PROFILE_INDEX_MOUNTAIN,
            'label': 'Mountain',
            'template': AM_BETA1_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_BETA1_RULE_PROFILE_INDEX_CITY,
            'label': 'City',
            'template': AM_BETA1_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_BETA1_RULE_PROFILE_INDEX_CIRCUIT,
            'label': 'Circuit',
            'template': AM_BETA1_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
    )
}


def _apply_ambeta1_semantic_rule_fields(
    row: bytearray,
    fields: Mapping[str, int],
    *,
    row_label: str,
) -> None:
    """Apply Beta1 semantic fields to one 26-byte rule row."""

    needed_players_default: int | None = None
    max_people_default: int | None = None

    for name, value in fields.items():
        if name == AM_BETA1_RULE_PACKED_FIELD_PLAYERS:
            row[AM_BETA1_RULE_OFFSET_PLAYERS_PACKED] = _coerce_rule_byte(
                value,
                label=f'{row_label} {AM_BETA1_RULE_PACKED_FIELD_PLAYERS}',
            )
            continue
        if name == AM_BETA1_RULE_PACKED_FIELD_NEEDED_PLAYERS_DEFAULT:
            needed_players_default = _coerce_rule_nibble(value, label=f'{row_label} {name}')
            continue
        if name == AM_BETA1_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT:
            max_people_default = _coerce_rule_nibble(value, label=f'{row_label} {name}')
            continue
        if name == AM_BETA1_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT_COUNT:
            max_people_default = _coerce_max_people_default_count(value, label=f'{row_label} {name}')
            continue
        if name not in AM_BETA1_RULE_SCALAR_FIELD_OFFSETS:
            raise ValueError(f'Unknown {row_label} field: {name}')
        row[AM_BETA1_RULE_SCALAR_FIELD_OFFSETS[name]] = _coerce_rule_byte(
            value,
            label=f'{row_label} field {name}',
        )

    if needed_players_default is not None or max_people_default is not None:
        existing_packed = row[AM_BETA1_RULE_OFFSET_PLAYERS_PACKED]
        row[AM_BETA1_RULE_OFFSET_PLAYERS_PACKED] = _pack_players_packed(
            needed_players_default=(
                (existing_packed >> 4) if needed_players_default is None else needed_players_default
            ),
            max_people_default=(
                (existing_packed & 0x0F) if max_people_default is None else max_people_default
            ),
            label=row_label,
        )


def serialize_ambeta1_rule_row(
    *,
    template: str,
    field_overrides: Mapping[str, int] | None = None,
    byte_overrides: Mapping[int, int] | None = None,
) -> bytes:
    """Serialize one Beta1 AM-USA-GAME-RULE row (26 bytes)."""

    if template not in AM_BETA1_RULE_ROW_TEMPLATES:
        raise ValueError(f'Unknown AM Beta1 rule template: {template}')

    row = bytearray(AM_BETA1_RULE_ROW_SIZE)
    fields = dict(AM_BETA1_RULE_ROW_TEMPLATES[template])
    if field_overrides is not None:
        fields.update(field_overrides)

    _apply_ambeta1_semantic_rule_fields(row, fields, row_label='beta1_rule')
    _apply_byte_overrides(
        row,
        byte_overrides,
        row_size=AM_BETA1_RULE_ROW_SIZE,
        row_label='beta1_rule',
    )
    return bytes(row)


def build_ambeta1_rule_csv_rows(
    config: Mapping[str, object] = AM_BETA1_GAME_RULE_CONFIG,
) -> tuple[str, ...]:
    """Build Beta1 AM-USA-GAME-RULE CSV rows (3 rows x 26 bytes)."""

    rule_profiles = tuple(_required_config_sequence(config, 'rule_profiles'))
    if len(rule_profiles) != AM_BETA1_RULE_PROFILE_ROW_COUNT:
        raise ValueError(
            'Beta1 rule config must define exactly '
            f'{AM_BETA1_RULE_PROFILE_ROW_COUNT} profiles, got {len(rule_profiles)}'
        )
    rows: list[str] = []
    for profile in rule_profiles:
        template = _required_rule_profile_str(profile, 'template')
        row = serialize_ambeta1_rule_row(
            template=template,
            field_overrides=_rule_profile_mapping(profile, 'field_overrides'),
            byte_overrides=_rule_profile_mapping(profile, 'byte_overrides'),
        )
        rows.append(row.hex())
    return tuple(rows)


def build_ambeta1_rule_page(
    config: Mapping[str, object] = AM_BETA1_GAME_RULE_CONFIG,
) -> str:
    """Build full Beta1 AM-USA-GAME-RULE page payload."""

    rule_profiles = _required_config_sequence(config, 'rule_profiles')
    csv_rows = build_ambeta1_rule_csv_rows(config)
    lines = [
        '<html><head>',
        '<!--AM-USA-GAME-RULE-->',
        '</head>',
    ]
    for profile in rule_profiles:
        index = _required_rule_profile_int(profile, 'index')
        label = _required_rule_profile_str(profile, 'label')
        lines.append(f'<!-- {index} {label} -->')
    lines.extend(
        (
            '<!--',
            '<CSV>',
        )
    )
    for row in csv_rows[:-1]:
        lines.append(f'"{row}",')
    lines.extend(
        (
            f'"{csv_rows[-1]}"',
            '</CSV>',
            '-->',
            '</html>',
            '',
        )
    )
    return '\n'.join(lines)


AM_BETA1_RULE_PAGE = build_ambeta1_rule_page()


class AutoModellistaBeta1WebModule(AutoModellistaWebModule):
    """Auto Modellista Beta1 web profile using the Beta1 AM-USA-GAME-RULE format."""

    name = 'automodellista_beta1'
    # Beta1 canonical AM-USA paths.
    info_path = '/amusa/info.html'
    rule_path = '/amusa/rule.html'
    rank_path = '/amusa/rank.html'
    taboo_path = '/amusa/taboo.html'
    upload_path = '/amusa/up.php'
    rule_page = AM_BETA1_RULE_PAGE
    # `ambeta1_bin/browser.bin` does not expose `AM-USA-GAME-TABOO`.
    taboo_page = None
