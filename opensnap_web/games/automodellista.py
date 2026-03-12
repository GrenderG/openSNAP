"""Auto Modellista web routes."""

import re
from collections.abc import Callable, Mapping, Sequence

from flask import Flask, Response, request

from opensnap_web.config import WebServerConfig
from opensnap_web.games.base import WebRouteTools
from opensnap_web.signup import SignupResult, SqliteSignupService

AM_INFO_PAGE = """<html><head>
<!--AM-USA-INFORMATION-->
</head>
<!--
<CSV>
"INFO_TAG = openSNAP",
"INFO_MSG = <BODY>Welcome to openSNAP!<END>",
</CSV>
-->
</html>
"""

# AM-USA-GAME-RULE parser contract recovered from `browser.bin` mode-9 path.
#
# `get_crs_shadow_data(9)` parses rows in this order:
# 1) 5 rows x 28 bytes each -> `BsGameRule` entries (stride 37 in memory)
# 2) 1 row x 64 bytes -> `BsPerformanceTbl`
# 3) terminator row (`"00"` in current page)
AM_RULE_PROFILE_ROW_COUNT = 5
AM_RULE_ROW_SIZE = 28
AM_PERFORMANCE_ROW_SIZE = 64
AM_RULE_CSV_TERMINATOR = '00'

# `Performance` (the 64-byte row parsed into `BsPerformanceTbl`) is not a
# race-rule menu profile.
#
# Runtime consumers in `SLUS_206.42`:
# - `check_performance` (`0x00284200`), called from `lbc_prelogin_00`, uses
#   this row during pre-login network diagnostics (DNS/ping retry flow,
#   thresholds, and score lookup).
# - `bro_mask_mv02` (`0x002a4840`) reads the first 16 bytes as eight halfwords
#   for browser-mask warning UI parameters.
# - `lbc_prelogin_02` (`0x00284d80`) reads `BsPerformanceTbl+0x28` as a gate
#   when packing/sending pre-login result fields.
#
# Confirmed `check_performance` offsets:
# - +0..+5, +0x26, +0x27, +0x29: control values used in the DNS/ping state flow.
# - +0x16/+0x17: 16-bit lookup-table entries used to build `MyPerformance`
#   from measured ping buckets.
#
# Important boundary note:
# - `check_performance` also reads `0x4b5d64..0x4b5d67`, which are NOT inside
#   this 64-byte row. They overlap `BsGameRule` row0 (`+0x14..+0x17`) and are
#   used as a fallback `MyPerformance` value.
#
# Parser side (`amus_bin/browser.bin`, mode 9) still only fills exactly these
# 64 bytes at `0x4b5d10`.

# Byte-level legend for known `BsGameRule` fields (the 28-byte rows in AM-USA-GAME-RULE).
#
# This is intentionally explicit so future RE passes can extend it instead of adding
# more "magic offsets" in serializer code.
#
# Source-backed use sites:
# - `To_ReadyBattle` patches/consumes +2 and +5.
# - `lbc_in_lobby_00` unpacks +19 into two defaults and reads +2.
# - Stock Event row differs at +26.
AM_RULE_ROW_FIELDS = {
    'course_mode_seed': {
        'offset': 2,
        'size': 1,
        'format': 'u8',
        'meaning': 'Course mode seed selected by menu/runtime mapping logic.',
    },
    'finish_grace_seconds': {
        'offset': 6,
        'size': 1,
        'format': 'u8(seconds)',
        'meaning': (
            'Post-first-finish grace timer in seconds. `netmsg_trans_netrule` '
            'stores `-1` when this byte is zero, otherwise `seconds * 60` frames.'
        ),
    },
    'lap_seed': {
        'offset': 5,
        'size': 1,
        'format': 'u8',
        'meaning': 'Lap selector seed; patched from current rule state before ready-battle send.',
    },
    'edit_mask': {
        'offset': 8,
        'size': 1,
        'format': 'u8',
        'meaning': 'Rule-editability/behavior mask used by stock rulesets.',
    },
    'players_packed': {
        'offset': 19,
        'size': 1,
        'format': 'u8(high_nibble=needed_players_default,low_nibble=max_people_default)',
        'meaning': 'Packed menu defaults used by rule-init logic.',
    },
    'event_flag': {
        'offset': 26,
        'size': 1,
        'format': 'u8',
        'meaning': 'Event profile discriminator in stock data (0=normal, 1=event).',
    },
}

AM_RULE_OFFSET_COURSE_MODE_SEED = int(AM_RULE_ROW_FIELDS['course_mode_seed']['offset'])
AM_RULE_OFFSET_FINISH_GRACE_SECONDS = int(AM_RULE_ROW_FIELDS['finish_grace_seconds']['offset'])
AM_RULE_OFFSET_LAP_SEED = int(AM_RULE_ROW_FIELDS['lap_seed']['offset'])
AM_RULE_OFFSET_EDIT_MASK = int(AM_RULE_ROW_FIELDS['edit_mask']['offset'])
AM_RULE_OFFSET_PLAYERS_PACKED = int(AM_RULE_ROW_FIELDS['players_packed']['offset'])
AM_RULE_OFFSET_EVENT_FLAG = int(AM_RULE_ROW_FIELDS['event_flag']['offset'])

# Unresolved offsets are intentionally tracked to prevent accidental "silent" reuse.
# These bytes are carried through untouched unless an explicit override is requested.
AM_RULE_KNOWN_OFFSETS = frozenset(
    (
        AM_RULE_OFFSET_COURSE_MODE_SEED,
        AM_RULE_OFFSET_FINISH_GRACE_SECONDS,
        AM_RULE_OFFSET_LAP_SEED,
        AM_RULE_OFFSET_EDIT_MASK,
        AM_RULE_OFFSET_PLAYERS_PACKED,
        AM_RULE_OFFSET_EVENT_FLAG,
    )
)
AM_RULE_UNKNOWN_OFFSETS = tuple(
    offset for offset in range(AM_RULE_ROW_SIZE) if offset not in AM_RULE_KNOWN_OFFSETS
)

# Semantic scalar fields map to one byte each.
AM_RULE_SCALAR_FIELD_OFFSETS = {
    'course_mode_seed': AM_RULE_OFFSET_COURSE_MODE_SEED,
    'finish_grace_seconds': AM_RULE_OFFSET_FINISH_GRACE_SECONDS,
    'lap_seed': AM_RULE_OFFSET_LAP_SEED,
    'edit_mask': AM_RULE_OFFSET_EDIT_MASK,
    'event_flag': AM_RULE_OFFSET_EVENT_FLAG,
}

# Byte +19 packs two 4-bit defaults:
# - high nibble: "Needed players" default index
# - low nibble: "No. of People" default index
AM_RULE_PACKED_FIELD_PLAYERS = 'players_packed'
AM_RULE_PACKED_FIELD_NEEDED_PLAYERS_DEFAULT = 'needed_players_default'
AM_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT = 'max_people_default'
# Count-oriented alias that enforces an editable 2..8 people range.
AM_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT_COUNT = 'max_people_default_count'

# Stock constants from observed AM-USA-GAME-RULE seed rows.
AM_RULE_COURSE_MODE_SEED_STOCK = 0x0A
AM_RULE_LAP_SEED_STOCK = 0x08
AM_RULE_FINISH_GRACE_SECONDS_STOCK = 0x0F
# Event profile uses a separate lap branch in `set_netrule_normal`:
# - reads `BsGameRule[row3].byte+5`
# - subtracts 1
# - writes that value as both lap min/max (locked)
# To lock Event lap to displayed value "3", byte +5 must be `0x03`.
AM_RULE_LAP_SEED_EVENT_LOCKED_THREE = 0x03
AM_RULE_EDIT_MASK_STOCK_EDITABLE = 0x01
AM_RULE_EVENT_FLAG_NORMAL = 0x00
AM_RULE_EVENT_FLAG_EVENT = 0x01

# Packed default index constants for byte +19.
#
# These are menu index defaults, not literal player counts.
# For example, Event uses 0x44, meaning both high/low default indexes are 4.
AM_RULE_DEFAULT_INDEX_STANDARD_NEEDED_PLAYERS = 0x02
AM_RULE_DEFAULT_INDEX_STANDARD_MAX_PEOPLE = 0x04
AM_RULE_DEFAULT_INDEX_EVENT_NEEDED_PLAYERS = 0x04
AM_RULE_DEFAULT_INDEX_EVENT_MAX_PEOPLE = 0x04
# Clubmeeting defaults below come from the stock AM-USA-GAME-RULE payload
# observed for release servers (packed byte +19 = 0x11), not from a hardcoded
# SLUS fallback constant. The ELF unpacks whatever nibble values are present in
# `BsGameRule[row=Clubmeeting].byte19`.
AM_RULE_DEFAULT_INDEX_CLUBMEETING_NEEDED_PLAYERS = 0x01
AM_RULE_DEFAULT_INDEX_CLUBMEETING_MAX_PEOPLE = 0x01
AM_RULE_DEFAULT_INDEX_PERFORMANCE_NEEDED_PLAYERS = 0x02
AM_RULE_DEFAULT_INDEX_PERFORMANCE_MAX_PEOPLE = 0x02

# Editable max-people range for standard race profiles.
AM_RULE_STANDARD_MAX_PEOPLE_MIN = 0x02
AM_RULE_STANDARD_MAX_PEOPLE_MAX = 0x08
# Keep stock default at 4 (template-level baseline).
AM_RULE_STANDARD_MAX_PEOPLE_DEFAULT = AM_RULE_DEFAULT_INDEX_STANDARD_MAX_PEOPLE
# Explicit override used by openSNAP standard profiles to unlock 2..8 editing.
#
# Important release-binary note (`SLUS_206.42`):
# - low nibble `0x8` is the sentinel that sends `set_netrule_normal`
#   (`0x00288c20..0x00288c70`) into the editable `2..8` branch;
# - it does NOT by itself make the displayed default start at `8`;
# - stock code seeds the current selection from `RoomInfo+4` via
#   `lb v1, 0(v1)` at `0x00288c54` (file offset `0x188dd4`) and then subtracts
#   `2`;
AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE = AM_RULE_STANDARD_MAX_PEOPLE_MAX
# Clubmeeting runtime override:
# - keep "Needed players" locked to 2 (same locked baseline as standard profiles),
# - force "No. of People" max nibble to 8 so `set_netrule_clubmeeting` follows
#   the editable 2..8 branch (`rule_join_min == 2 && rule_join_max == 8`).
AM_RULE_CLUBMEETING_NEEDED_PLAYERS_LOCKED_OVERRIDE = AM_RULE_DEFAULT_INDEX_STANDARD_NEEDED_PLAYERS
AM_RULE_CLUBMEETING_MAX_PEOPLE_EDITABLE_OVERRIDE = AM_RULE_STANDARD_MAX_PEOPLE_MAX

AM_RULE_TEMPLATE_NAME_NORMAL = 'normal'
AM_RULE_TEMPLATE_NAME_BLANK = 'blank'

# Template used by stock Mountain/City/Circuit rows.
# Byte-level defaults:
# - +2  (`course_mode_seed`) = 0x0A
# - +5  (`lap_seed`) = 0x08
# - +6  (`finish_grace_seconds`) = 0x0F
# - +8  (`edit_mask`) = 0x01
# - +19 (`players_packed`) high/low defaults = 0x2/0x4 -> packed 0x24
# - +26 (`event_flag`) = 0
AM_RULE_TEMPLATE_NORMAL = {
    'course_mode_seed': AM_RULE_COURSE_MODE_SEED_STOCK,
    'lap_seed': AM_RULE_LAP_SEED_STOCK,
    'finish_grace_seconds': AM_RULE_FINISH_GRACE_SECONDS_STOCK,
    'edit_mask': AM_RULE_EDIT_MASK_STOCK_EDITABLE,
    'needed_players_default': AM_RULE_DEFAULT_INDEX_STANDARD_NEEDED_PLAYERS,
    'max_people_default': AM_RULE_DEFAULT_INDEX_STANDARD_MAX_PEOPLE,
    'event_flag': AM_RULE_EVENT_FLAG_NORMAL,
}

# Blank template starts all 28 bytes as zero and relies on profile overrides.
AM_RULE_TEMPLATE_BLANK: dict[str, int] = {}

# Named template catalog used by `AM_GAME_RULE_CONFIG`.
AM_RULE_ROW_TEMPLATES = {
    AM_RULE_TEMPLATE_NAME_NORMAL: AM_RULE_TEMPLATE_NORMAL,
    AM_RULE_TEMPLATE_NAME_BLANK: AM_RULE_TEMPLATE_BLANK,
}

# `SLUS_206.42` tables:
# - rule_title_tbl @ 0x370da0
# - rule_choice_suu_tbl @ 0x3b34c0
# - rule_default_tbl @ 0x3b34d0
# - stage_name_tbl/course_title_tbl_* @ 0x3b3500/0x370dc0+
AM_RULE_TITLES = (
    'Course Settings',
    'Needed players',
    'No. of People',
    'Lap',
    'Boost',
)

AM_RULE_COURSE_TYPES = ('mountain', 'city', 'circuit')
AM_RULE_NAMES = ('course_settings', 'needed_players', 'no_of_people', 'lap', 'boost')

# `rule_choice_suu_tbl` raw bytes at `0x003b34c0` (5 rules x 3 course types):
# 04 08 05  01 01 01  03 07 07  01 04 04  02 02 02
AM_RULE_CHOICE_COUNT_MATRIX = (
    (4, 8, 5),
    (1, 1, 1),
    (3, 7, 7),
    (1, 4, 4),
    (2, 2, 2),
)

# `rule_default_tbl` raw bytes at `0x003b34d0` (5 rules x 3 course types):
# 00 00 00  00 00 00  00 00 00  00 01 01  00 00 00
AM_RULE_DEFAULT_INDEX_MATRIX = (
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 1, 1),
    (0, 0, 0),
)

AM_COURSES_MOUNTAIN = (
    'Rokko Hill Climb',
    'Rokko Downhill',
    'Akagi Hill Climb',
    'Akagi Downhill',
)
AM_COURSES_CITY = (
    'W.Tokyo',
    'W.Tokyo <R>',
    'E.Tokyo(Fine)',
    'E.Tokyo(Fine)<R>',
    'E.Tokyo(Rain)',
    'E.Tokyo(Rain)<R>',
    'Osaka Hi.Way',
    'Osaka Hi.Way <R>',
)
AM_COURSES_CIRCUIT = (
    'Suzuka',
    'US Speed Way',
    'US Dirt Track',
    'Tamiya',
    'Tamiya <R>',
)

AM_COURSES_BY_TYPE = {
    'mountain': AM_COURSES_MOUNTAIN,
    'city': AM_COURSES_CITY,
    'circuit': AM_COURSES_CIRCUIT,
}

# Rule option lists per course type.
#
# `course_settings` uses confirmed course-title tables.
# For several non-course rules, this RE pass confirmed exact counts but not all
# text labels. Those are tracked as stable index placeholders (`choice_idx_N`) so
# the config remains explicit and count-safe.
# These tuples are symbolic option-slot ids only (not on-wire values); they preserve
# menu cardinality and stable ordering for unresolved label tables.
AM_RULE_OPTION_INDEXES_SINGLE = ('choice_idx_0',)
AM_RULE_OPTION_INDEXES_TRIPLE = ('choice_idx_0', 'choice_idx_1', 'choice_idx_2')
AM_RULE_OPTION_INDEXES_QUAD = ('choice_idx_0', 'choice_idx_1', 'choice_idx_2', 'choice_idx_3')
AM_RULE_OPTION_INDEXES_SEPTUPLE = (
    'choice_idx_0',
    'choice_idx_1',
    'choice_idx_2',
    'choice_idx_3',
    'choice_idx_4',
    'choice_idx_5',
    'choice_idx_6',
)
AM_RULE_OPTION_BOOST = ('boost_off', 'boost_on')

AM_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE = {
    # rule_choice_suu_tbl column 0 => counts 4,1,3,1,2
    'mountain': {
        'course_settings': AM_COURSES_MOUNTAIN,
        'needed_players': AM_RULE_OPTION_INDEXES_SINGLE,
        'no_of_people': AM_RULE_OPTION_INDEXES_TRIPLE,
        'lap': AM_RULE_OPTION_INDEXES_SINGLE,
        'boost': AM_RULE_OPTION_BOOST,
    },
    # rule_choice_suu_tbl column 1 => counts 8,1,7,4,2
    'city': {
        'course_settings': AM_COURSES_CITY,
        'needed_players': AM_RULE_OPTION_INDEXES_SINGLE,
        'no_of_people': AM_RULE_OPTION_INDEXES_SEPTUPLE,
        'lap': AM_RULE_OPTION_INDEXES_QUAD,
        'boost': AM_RULE_OPTION_BOOST,
    },
    # rule_choice_suu_tbl column 2 => counts 5,1,7,4,2
    'circuit': {
        'course_settings': AM_COURSES_CIRCUIT,
        'needed_players': AM_RULE_OPTION_INDEXES_SINGLE,
        'no_of_people': AM_RULE_OPTION_INDEXES_SEPTUPLE,
        'lap': AM_RULE_OPTION_INDEXES_QUAD,
        'boost': AM_RULE_OPTION_BOOST,
    },
}

AM_RULE_CHOICE_COUNTS = {
    rule_name: {
        course_type: AM_RULE_CHOICE_COUNT_MATRIX[rule_index][course_type_index]
        for course_type_index, course_type in enumerate(AM_RULE_COURSE_TYPES)
    }
    for rule_index, rule_name in enumerate(AM_RULE_NAMES)
}

AM_RULE_CHOICE_COUNTS_BY_COURSE_TYPE = {
    course_type: {rule_name: AM_RULE_CHOICE_COUNTS[rule_name][course_type] for rule_name in AM_RULE_NAMES}
    for course_type in AM_RULE_COURSE_TYPES
}

# Validate that human-readable option catalogs keep matching the binary row counts.
for _course_type, _rule_options in AM_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE.items():
    for _rule_name, _options in _rule_options.items():
        if len(_options) != AM_RULE_CHOICE_COUNTS_BY_COURSE_TYPE[_course_type][_rule_name]:
            raise ValueError(
                f'AM rule choice option length mismatch: '
                f'{_course_type}.{_rule_name} has {len(_options)} options, '
                f'expected {AM_RULE_CHOICE_COUNTS_BY_COURSE_TYPE[_course_type][_rule_name]}'
            )

AM_RULE_DEFAULT_INDEXES = {
    rule_name: {
        course_type: AM_RULE_DEFAULT_INDEX_MATRIX[rule_index][course_type_index]
        for course_type_index, course_type in enumerate(AM_RULE_COURSE_TYPES)
    }
    for rule_index, rule_name in enumerate(AM_RULE_NAMES)
}

AM_RULE_DEFAULT_INDEXES_BY_COURSE_TYPE = {
    course_type: {rule_name: AM_RULE_DEFAULT_INDEXES[rule_name][course_type] for rule_name in AM_RULE_NAMES}
    for course_type in AM_RULE_COURSE_TYPES
}

AM_RULE_MENU_METADATA = {
    'rule_titles': AM_RULE_TITLES,
    'rule_names': AM_RULE_NAMES,
    'course_types': AM_RULE_COURSE_TYPES,
    'choice_count_matrix': AM_RULE_CHOICE_COUNT_MATRIX,
    'default_index_matrix': AM_RULE_DEFAULT_INDEX_MATRIX,
    'choice_counts': AM_RULE_CHOICE_COUNTS,
    'choice_options_by_course_type': AM_RULE_CHOICE_OPTIONS_BY_COURSE_TYPE,
    'choice_counts_by_course_type': AM_RULE_CHOICE_COUNTS_BY_COURSE_TYPE,
    'default_indexes': AM_RULE_DEFAULT_INDEXES,
    'default_indexes_by_course_type': AM_RULE_DEFAULT_INDEXES_BY_COURSE_TYPE,
    'courses_by_type': AM_COURSES_BY_TYPE,
}

AM_RULE_PROFILE_INDEX_MOUNTAIN = 0
AM_RULE_PROFILE_INDEX_CITY = 1
AM_RULE_PROFILE_INDEX_CIRCUIT = 2
AM_RULE_PROFILE_INDEX_EVENT = 3
AM_RULE_PROFILE_INDEX_CLUBMEETING = 4
AM_RULE_PROFILE_INDEX_PERFORMANCE = 5

# AM-USA-GAME-RULE definition used to build the `<CSV>` body.
#
# Human-readable profile rules:
# - Mountain/City/Circuit use stock `normal` template.
# - Index 3 is consumed by Clubmeeting lobby/menu rule init
#   (`To_EnterClubMeeting` sets lobby id 0x14 -> `lbc_in_lobby_00` selects row 3).
#   We keep event flag byte +26 set to 1, but force packed defaults to 0x28 so
#   Clubmeeting follows the editable 2..8 branch in `set_netrule_clubmeeting`.
# - Index 4 is the separate active Clubmeeting rule row used by `To_ReadyBattle`
#   via the lobby-id lookup table at `0x003710df + lobby_id`. For lobby id `20`,
#   that table selects row `4`, so byte `+8` must also keep chat enabled there.
# - Performance row is separate (64 bytes) and currently seeds only known fields.
#
# Config contract:
# - `template`: named base row defaults (`normal` or `blank`).
# - `field_overrides`: semantic field writes (preferred).
#   - `max_people_default_count` is a convenience alias for standard profiles and
#     is validated to `2..8` before packing into byte `+19` low nibble.
# - `byte_overrides`: raw offset writes for fields not yet semantically mapped.
AM_GAME_RULE_CONFIG = {
    'rule_profiles': (
        {
            'index': AM_RULE_PROFILE_INDEX_MOUNTAIN,
            'label': 'Mountain',
            'template': AM_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                # Override byte +19 low nibble to `8` so release `set_netrule_normal`
                # enters its editable `2..8` branch. The displayed default still
                # comes from the binary-side selection seed, not directly from this
                # nibble.
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_RULE_PROFILE_INDEX_CITY,
            'label': 'City',
            'template': AM_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                # Override byte +19 low nibble to `8` so release `set_netrule_normal`
                # enters its editable `2..8` branch. The displayed default still
                # comes from the binary-side selection seed, not directly from this
                # nibble.
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_RULE_PROFILE_INDEX_CIRCUIT,
            'label': 'Circuit',
            'template': AM_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                # Override byte +19 low nibble to `8` so release `set_netrule_normal`
                # enters its editable `2..8` branch. The displayed default still
                # comes from the binary-side selection seed, not directly from this
                # nibble.
                'max_people_default_count': AM_RULE_STANDARD_MAX_PEOPLE_EDITABLE_OVERRIDE,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_RULE_PROFILE_INDEX_EVENT,
            'label': 'Event / Clubmeeting runtime',
            'template': AM_RULE_TEMPLATE_NAME_NORMAL,
            'field_overrides': {
                # Event password row/value ("No PW") is not driven by this
                # AM-USA-GAME-RULE row. `set_netrule_normal` hard-sets room
                # title/password control bytes (`0x551630/0x551631`) and keeps
                # that menu behavior client-side for this path.
                # Event branch (`set_netrule_normal`, row index 3) consumes
                # byte +5 as a 1-based lap seed and then locks lap to that
                # resulting min/max value. `0x03` yields locked lap=3.
                'lap_seed': AM_RULE_LAP_SEED_EVENT_LOCKED_THREE,
                # Byte +19 high nibble -> Needed players default index.
                # Byte +19 low nibble -> No. of People default index.
                # Stock event row is 0x44, but Clubmeeting runtime consumes this
                # index and requires 0x28 for editable No. of People (2..8).
                'needed_players_default': AM_RULE_CLUBMEETING_NEEDED_PLAYERS_LOCKED_OVERRIDE,
                'max_people_default_count': AM_RULE_CLUBMEETING_MAX_PEOPLE_EDITABLE_OVERRIDE,
                # Byte +26 marks this profile as Event in stock data.
                'event_flag': AM_RULE_EVENT_FLAG_EVENT,
            },
            'byte_overrides': {},
        },
        {
            'index': AM_RULE_PROFILE_INDEX_CLUBMEETING,
            'label': 'Clubmeeting active runtime',
            'template': AM_RULE_TEMPLATE_NAME_BLANK,
            'field_overrides': {
                'finish_grace_seconds': AM_RULE_FINISH_GRACE_SECONDS_STOCK,
                # Release `player_gallery` (`Start -> Triangle`) gates
                # `netmsg_push_chat` on runtime byte `0x4abd7a`, which comes
                # from rule byte `+8`. Club Meeting active rule selection uses
                # row 4, not row 3, through `To_ReadyBattle`'s lobby-id table.
                'edit_mask': AM_RULE_EDIT_MASK_STOCK_EDITABLE,
                # Stock AM-USA-GAME-RULE payload uses packed defaults 0x11.
                'needed_players_default': AM_RULE_DEFAULT_INDEX_CLUBMEETING_NEEDED_PLAYERS,
                'max_people_default': AM_RULE_DEFAULT_INDEX_CLUBMEETING_MAX_PEOPLE,
            },
            'byte_overrides': {},
        },
    ),
    'performance_profile': {
        'index': AM_RULE_PROFILE_INDEX_PERFORMANCE,
        'label': 'Performance',
        # Performance row is 64 bytes and is consumed by pre-login connectivity
        # logic (`check_performance`) plus browser-mask UI logic (`bro_mask_mv02`).
        #
        # Field keys below are serializer aliases for specific byte positions.
        # They are retained to avoid duplicating row-writer code, but these names
        # do not describe gameplay-rule semantics in this row.
        #
        # Stock/default seeded bytes in openSNAP:
        # - +2  -> 0x0A
        # - +5  -> 0x08
        # - +8  -> 0x01
        # - +19 -> packed 0x22
        #
        # Unmapped bytes stay zero unless explicitly overridden.
        'field_overrides': {
            'course_mode_seed': AM_RULE_COURSE_MODE_SEED_STOCK,
            'lap_seed': AM_RULE_LAP_SEED_STOCK,
            'edit_mask': AM_RULE_EDIT_MASK_STOCK_EDITABLE,
            'needed_players_default': AM_RULE_DEFAULT_INDEX_PERFORMANCE_NEEDED_PLAYERS,
            'max_people_default': AM_RULE_DEFAULT_INDEX_PERFORMANCE_MAX_PEOPLE,
        },
        'byte_overrides': {
            # Keep open for low-level tuning when more performance bytes are decoded.
        },
    },
}

AM_RANK_PAGE = '<html><body>am_rank</body></html>\n'
# `am_taboo.html` is not a human-facing release page.
#
# Reverse-engineered release mechanism:
# - `amus_bin/browser.bin` maps `AM-USA-GAME-TABOO` to special-tag type `0x27`,
#   then `special_tag_check` arms parser mode `12`.
# - after a `<CSV>` marker, `get_crs_shadow_data(12)` decodes the page into the
#   `Net_Kinshi_buff` table used by `cmn_mongon_check` as an extra downloadable
#   taboo-word list layered on top of the game's built-in word filters.
# - the decoded table uses the same packed `check_mongon` layout as the retail
#   static list: `14` text bytes per slot plus metadata/continuation state in
#   byte `15`, allowing long taboo phrases to span multiple `16`-byte records.
#
# So the original server-side contract is effectively:
# - `<!--AM-USA-GAME-TABOO-->`
# - one multi-line quoted `<CSV>` field containing taboo phrases in that packed
#   transport format.
#
# This placeholder intentionally omits that contract. It keeps the fetch path
# live without populating the downloadable taboo table, so only the game's
# built-in filters remain active in openSNAP for now.
AM_TABOO_PAGE = '<html><body>am_taboo</body></html>\n'
# `patch*.html` is not a cosmetic page family in either Beta1 or release.
#
# Reverse-engineered browser contract:
# - `AM-USA-GAME-PROG` is special-tag type `0x26` in both overlays.
# - `special_tag_check` maps that to parser mode `11`.
# - once the page also exposes a `<CSV>` block, `get_crs_shadow_data(11)`
#   decodes the numeric payload directly into the same `0x20000` patch buffer
#   later executed by main-ELF `PatchExec`.
# - the first CSV byte is a fixed chunk id `'1'..'5'`; chunks 1..4 fill
#   `0x7000` bytes each and chunk 5 fills the final `0x4000`.
#
# So:
# - Beta1 `/amusa/patch1.html` .. `/amusa/patch5.html`
# - Release `/amusa/patch/2/am_patch1.html` .. `/amusa/patch/2/am_patch5.html`
# are five transport shards of one runtime patch program, not patch notes.
#
# These placeholder pages are intentionally inert because they do not provide
# the original `<!--AM-USA-GAME-PROG-->` + `<CSV>` payload contract.
AM_PATCH1_PAGE = '<html><body>This is test patch1.html file</body></html>\n'
AM_PATCH2_PAGE = '<html><body>This is test patch2.html file</body></html>\n'
AM_PATCH3_PAGE = '<html><body>This is test patch3.html file</body></html>\n'
AM_PATCH4_PAGE = '<html><body>This is test patch4.html file</body></html>\n'
AM_PATCH5_PAGE = '<html><body>This is test patch5.html file</body></html>\n'
MIN_USERNAME_LENGTH = 4
MAX_USERNAME_LENGTH = 15
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 15
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9_]{4,15}$')
SIGNUP_INDEX_PAGE = (
    '<html>\n'
    '<body>\n'
    'openSNAP signup service<br>\n'
    '<br>\n'
    'Choose the username to save on your memory card.<br>\n'
    '<br>\n'
    '<form action="create_id.html" method="post">\n'
    'Username: '
    f'<input type="text" name="username" size="{MAX_USERNAME_LENGTH}" maxlength="{MAX_USERNAME_LENGTH}">\n'
    '<br>\n'
    'Password: '
    f'<input type="password" name="password" size="{MAX_PASSWORD_LENGTH}" maxlength="{MAX_PASSWORD_LENGTH}">\n'
    '<br>\n'
    '<input type="submit" value="Create/Login ID">\n'
    '</form>\n'
    '</body>\n'
    '</html>\n'
)


def serialize_am_rule_row(
    *,
    template: str,
    field_overrides: Mapping[str, int] | None = None,
    byte_overrides: Mapping[int, int] | None = None,
) -> bytes:
    """Serialize one 28-byte AM-USA-GAME-RULE row from semantic fields.

    Flow:
    1) start with all-zero row;
    2) apply template semantic defaults;
    3) apply semantic overrides by field name;
    4) apply optional raw byte overrides as last-write-wins.
    """

    if template not in AM_RULE_ROW_TEMPLATES:
        raise ValueError(f'Unknown AM rule template: {template}')

    row = bytearray(AM_RULE_ROW_SIZE)
    fields = dict(AM_RULE_ROW_TEMPLATES[template])
    if field_overrides is not None:
        fields.update(field_overrides)

    _apply_semantic_rule_fields(
        row,
        fields,
        row_label='rule',
    )

    _apply_byte_overrides(
        row,
        byte_overrides,
        row_size=AM_RULE_ROW_SIZE,
        row_label='rule',
    )

    return bytes(row)


def serialize_am_performance_row(
    *,
    field_overrides: Mapping[str, int] | None = None,
    byte_overrides: Mapping[int, int] | None = None,
) -> bytes:
    """Serialize the 64-byte AM-USA-GAME-RULE performance row.

    This row feeds pre-login network diagnostics (`check_performance`) and
    browser warning UI behavior, not the race-rule menu itself.

    For serializer reuse we apply the same field keys used by rule rows for
    byte placement (`+2`, `+5`, `+8`, packed `+19`), but those names do not
    imply the same gameplay semantics inside the performance row.

    Remaining bytes stay zero unless overridden explicitly via `byte_overrides`.
    """

    row = bytearray(AM_PERFORMANCE_ROW_SIZE)
    _apply_semantic_rule_fields(
        row,
        field_overrides or {},
        row_label='performance',
        allow_event_flag=False,
    )
    _apply_byte_overrides(
        row,
        byte_overrides,
        row_size=AM_PERFORMANCE_ROW_SIZE,
        row_label='performance',
    )

    return bytes(row)


def build_am_rule_csv_rows(
    config: Mapping[str, object] = AM_GAME_RULE_CONFIG,
) -> tuple[str, ...]:
    """Build AM-USA-GAME-RULE CSV rows from profile definitions.

    Output order must match `browser.bin` mode-9 parser contract:
    - 5x rule rows (28 bytes each),
    - 1x performance row (64 bytes),
    - 1x terminator row ("00").
    """

    rule_profiles = _required_config_sequence(config, 'rule_profiles')
    performance_profile = _required_config_mapping(config, 'performance_profile')
    rows: list[str] = []
    for profile in rule_profiles:
        template = _required_rule_profile_str(profile, 'template')
        field_overrides = _rule_profile_mapping(profile, 'field_overrides')
        byte_overrides = _rule_profile_mapping(profile, 'byte_overrides')
        rule_blob = serialize_am_rule_row(
            template=template,
            field_overrides=field_overrides,
            byte_overrides=byte_overrides,
        )
        rows.append(rule_blob.hex())
    performance_field_overrides = _rule_profile_mapping(performance_profile, 'field_overrides')
    performance_row = serialize_am_performance_row(
        field_overrides=performance_field_overrides,
        byte_overrides=_rule_profile_mapping(performance_profile, 'byte_overrides'),
    )
    rows.append(performance_row.hex())
    rows.append(AM_RULE_CSV_TERMINATOR)
    return tuple(rows)


def build_am_rule_page(
    config: Mapping[str, object] = AM_GAME_RULE_CONFIG,
) -> str:
    """Build the full AM-USA-GAME-RULE HTML payload from rule profiles."""

    rule_profiles = _required_config_sequence(config, 'rule_profiles')
    performance_profile = _required_config_mapping(config, 'performance_profile')
    csv_rows = build_am_rule_csv_rows(config)
    lines = [
        '<html><head>',
        '<!--AM-USA-GAME-RULE-->',
        '</head>',
    ]
    for profile in rule_profiles:
        index = _required_rule_profile_int(profile, 'index')
        label = _required_rule_profile_str(profile, 'label')
        lines.append(f'<!-- {index} {label} -->')
    perf_index = _required_rule_profile_int(performance_profile, 'index')
    perf_label = _required_rule_profile_str(performance_profile, 'label')
    lines.append(f'<!-- {perf_index} {perf_label} -->')
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


def _coerce_rule_byte(value: int, *, label: str) -> int:
    """Validate a byte value used in AM-USA-GAME-RULE rows."""

    if not isinstance(value, int):
        raise ValueError(f'Rule {label} must be int, got {value!r}')
    if value < 0 or value > 0xFF:
        raise ValueError(f'Rule {label} must be in range 0..255, got {value!r}')
    return value


def _coerce_rule_nibble(value: int, *, label: str) -> int:
    """Validate a 4-bit value used in packed AM rule defaults."""

    if not isinstance(value, int):
        raise ValueError(f'Rule {label} must be int, got {value!r}')
    if value < 0 or value > 0x0F:
        raise ValueError(f'Rule {label} must be in range 0..15, got {value!r}')
    return value


def _coerce_max_people_default_count(value: int, *, label: str) -> int:
    """Validate editable max-people default count for standard race profiles."""

    nibble = _coerce_rule_nibble(value, label=label)
    if nibble < AM_RULE_STANDARD_MAX_PEOPLE_MIN or nibble > AM_RULE_STANDARD_MAX_PEOPLE_MAX:
        raise ValueError(
            f'Rule {label} must be in range '
            f'{AM_RULE_STANDARD_MAX_PEOPLE_MIN}..{AM_RULE_STANDARD_MAX_PEOPLE_MAX}, got {value!r}'
        )
    return nibble


def _pack_players_packed(
    *,
    needed_players_default: int,
    max_people_default: int,
    label: str,
) -> int:
    """Pack `needed_players_default` and `max_people_default` into byte +19."""

    needed_nibble = _coerce_rule_nibble(needed_players_default, label=f'{label} needed_players_default')
    max_nibble = _coerce_rule_nibble(max_people_default, label=f'{label} max_people_default')
    return (needed_nibble << 4) | max_nibble


def _apply_semantic_rule_fields(
    row: bytearray,
    fields: Mapping[str, int],
    *,
    row_label: str,
    allow_event_flag: bool = True,
) -> None:
    """Apply semantic AM rule fields to a row buffer.

    `needed_players_default` and `max_people_default` are staged and packed
    together into byte +19 after scalar fields are processed.
    """

    needed_players_default: int | None = None
    max_people_default: int | None = None

    for name, value in fields.items():
        if name == AM_RULE_PACKED_FIELD_PLAYERS:
            row[AM_RULE_OFFSET_PLAYERS_PACKED] = _coerce_rule_byte(
                value,
                label=f'{row_label} {AM_RULE_PACKED_FIELD_PLAYERS}',
            )
            continue
        if name == AM_RULE_PACKED_FIELD_NEEDED_PLAYERS_DEFAULT:
            needed_players_default = _coerce_rule_nibble(value, label=f'{row_label} {name}')
            continue
        if name == AM_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT:
            max_people_default = _coerce_rule_nibble(value, label=f'{row_label} {name}')
            continue
        if name == AM_RULE_PACKED_FIELD_MAX_PEOPLE_DEFAULT_COUNT:
            max_people_default = _coerce_max_people_default_count(value, label=f'{row_label} {name}')
            continue
        if name == 'event_flag' and not allow_event_flag:
            raise ValueError(f'{row_label} does not support event_flag')
        if name not in AM_RULE_SCALAR_FIELD_OFFSETS:
            raise ValueError(f'Unknown {row_label} field: {name}')
        offset = AM_RULE_SCALAR_FIELD_OFFSETS[name]
        row[offset] = _coerce_rule_byte(value, label=f'{row_label} field {name}')

    if needed_players_default is not None or max_people_default is not None:
        existing_packed = row[AM_RULE_OFFSET_PLAYERS_PACKED]
        row[AM_RULE_OFFSET_PLAYERS_PACKED] = _pack_players_packed(
            needed_players_default=(
                (existing_packed >> 4) if needed_players_default is None else needed_players_default
            ),
            max_people_default=(
                (existing_packed & 0x0F) if max_people_default is None else max_people_default
            ),
            label=row_label,
        )


def _apply_byte_overrides(
    row: bytearray,
    byte_overrides: Mapping[int, int] | None,
    *,
    row_size: int,
    row_label: str,
) -> None:
    """Apply validated byte overrides to a row buffer."""

    if byte_overrides is None:
        return
    for offset, value in byte_overrides.items():
        if not isinstance(offset, int):
            raise ValueError(f'{row_label} byte offset must be int, got {offset!r}')
        if offset < 0 or offset >= row_size:
            raise ValueError(f'{row_label} byte offset out of range: {offset}')
        row[offset] = _coerce_rule_byte(value, label=f'{row_label} offset {offset}')


def _required_rule_profile_str(profile: Mapping[str, object], key: str) -> str:
    """Read a required string key from a rule profile."""

    value = profile.get(key)
    if not isinstance(value, str):
        raise ValueError(f'Rule profile key {key!r} must be a string')
    return value


def _required_rule_profile_int(profile: Mapping[str, object], key: str) -> int:
    """Read a required integer key from a rule profile."""

    value = profile.get(key)
    if not isinstance(value, int):
        raise ValueError(f'Rule profile key {key!r} must be an int')
    return value


def _rule_profile_mapping(profile: Mapping[str, object], key: str) -> Mapping:
    """Read an optional mapping key from a rule profile."""

    value = profile.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f'Rule profile key {key!r} must be a mapping')
    return value


def _required_config_sequence(config: Mapping[str, object], key: str) -> Sequence[Mapping[str, object]]:
    """Read a required sequence of rule-profile mappings from config."""

    value = config.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f'Rule config key {key!r} must be a sequence')
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f'Rule config key {key!r} must contain mappings')
    return value


def _required_config_mapping(config: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Read a required mapping from rule config."""

    value = config.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f'Rule config key {key!r} must be a mapping')
    return value


AM_RULE_PAGE = build_am_rule_page()


def register_signup_routes(
    app: Flask,
    *,
    tools: WebRouteTools,
    signup_service: SqliteSignupService,
    route_prefixes: tuple[str, ...],
    include_root_aliases: bool,
    host: str | None = None,
    endpoint_namespace: str = '',
) -> None:
    """Register SNAP signup/create-id routes for one or more path prefixes."""

    normalized_prefixes = tuple(prefix.strip('/') for prefix in route_prefixes if prefix.strip('/'))
    if not normalized_prefixes:
        return
    endpoint_prefix = f'{endpoint_namespace}_' if endpoint_namespace else ''

    if include_root_aliases:
        app.add_url_rule(
            '/',
            endpoint=f'{endpoint_prefix}signup_root_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
            host=host,
        )
        app.add_url_rule(
            '/login.php',
            endpoint=f'{endpoint_prefix}signup_login_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
            host=host,
        )

    for prefix in normalized_prefixes:
        path_prefix = prefix.replace('/', '_')
        app.add_url_rule(
            f'/{prefix}/',
            endpoint=f'{endpoint_prefix}signup_{path_prefix}_index_root',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
            host=host,
        )
        app.add_url_rule(
            f'/{prefix}/index.jsp',
            endpoint=f'{endpoint_prefix}signup_{path_prefix}_index',
            methods=['GET'],
            view_func=_make_signup_index_view(tools),
            host=host,
        )
        app.add_url_rule(
            f'/{prefix}/create_id.html',
            endpoint=f'{endpoint_prefix}signup_{path_prefix}_create_id_query',
            methods=['GET', 'POST'],
            view_func=_make_signup_query_view(signup_service),
            host=host,
        )
        app.add_url_rule(
            f'/{prefix}/create_id_<username>.html',
            endpoint=f'{endpoint_prefix}signup_{path_prefix}_create_id_dynamic',
            methods=['GET'],
            view_func=_make_signup_dynamic_view(signup_service),
            host=host,
        )


def _make_signup_index_view(tools: WebRouteTools) -> Callable[[], Response]:
    """Build one index handler for the original signup pages."""

    def _signup_index(**_kwargs: str) -> Response:
        return tools.html_response(SIGNUP_INDEX_PAGE)

    return _signup_index


def _make_signup_query_view(signup_service: SqliteSignupService) -> Callable[[], Response]:
    """Build query/create-id handler using username from request values."""

    def _signup_query(**_kwargs: str) -> Response:
        username = (request.values.get('username') or '').strip()
        password = (request.values.get('password') or '').strip()
        return _build_signup_response(
            username=username,
            password=password,
            signup_service=signup_service,
        )

    return _signup_query


def _make_signup_dynamic_view(signup_service: SqliteSignupService) -> Callable[[str], Response]:
    """Build dynamic create-id handler using username from route path."""

    def _signup_dynamic(username: str, **_kwargs: str) -> Response:
        password = (request.values.get('password') or '').strip()
        return _build_signup_response(
            username=username.strip(),
            password=password,
            signup_service=signup_service,
        )

    return _signup_dynamic


def _make_static_page_view(tools: WebRouteTools, page: str) -> Callable[[], Response]:
    """Build a static HTML response view for a pre-rendered page payload."""

    def _static_page(**_kwargs: str) -> Response:
        return tools.html_response(page)

    return _static_page


def _make_upload_view(tools: WebRouteTools) -> Callable[[], Response]:
    """Build the ranking upload stub handler used by AM clients."""

    def _upload(**_kwargs: str) -> Response:
        # `nwPBRanking` uses the embedded `/amusa/am_up.php` path after the
        # post-game room transition. Keep this stub endpoint available until
        # the exact upload and response body are fully decoded.
        tools.dump_request('Handled Auto Modellista ranking upload request.')
        return Response('', mimetype='text/plain')

    return _upload


class AutoModellistaWebModule:
    """Web endpoints used by Auto Modellista clients."""

    name = 'automodellista'
    signup_route_prefixes = ('amweb', 'ftpublicbeta/reg')
    include_signup_root_aliases = True
    # Release/Beta2 canonical AM-USA paths.
    info_path = '/amusa/am_info.html'
    rule_path = '/amusa/am_rule.html'
    rank_path = '/amusa/am_rank.html'
    taboo_path = '/amusa/am_taboo.html'
    upload_path = '/amusa/am_up.php'
    info_page = AM_INFO_PAGE
    rule_page = AM_RULE_PAGE
    rank_page = AM_RANK_PAGE
    taboo_page = AM_TABOO_PAGE
    patch_pages = {
        1: AM_PATCH1_PAGE,
        2: AM_PATCH2_PAGE,
        3: AM_PATCH3_PAGE,
        4: AM_PATCH4_PAGE,
        5: AM_PATCH5_PAGE,
    }

    def _static_page_specs(self) -> tuple[tuple[str, str, str], ...]:
        """Return core static endpoint/page route specs with singular paths."""

        specs: list[tuple[str, str, str]] = [
            ('info', self.info_path, self.info_page),
            ('rule', self.rule_path, self.rule_page),
            ('rank', self.rank_path, self.rank_page),
        ]
        if self.taboo_page:
            specs.append(('taboo', self.taboo_path, self.taboo_page))
        return tuple(specs)

    def register_routes(
        self,
        app: Flask,
        config: WebServerConfig,
        tools: WebRouteTools,
        *,
        host: str | None = None,
    ) -> None:
        """Register Auto Modellista-specific web endpoints."""

        del config
        signup_service = SqliteSignupService()
        register_signup_routes(
            app,
            tools=tools,
            signup_service=signup_service,
            route_prefixes=self.signup_route_prefixes,
            include_root_aliases=self.include_signup_root_aliases,
            host=host,
            endpoint_namespace=self.name,
        )
        for spec_index, (slug, path, page) in enumerate(self._static_page_specs()):
            view = _make_static_page_view(tools, page)
            app.add_url_rule(
                path,
                endpoint=f'{self.name}_{slug}_{spec_index}',
                methods=['GET'],
                view_func=view,
                host=host,
            )

        # Keep both patch URL families alive.
        #
        # Binary-backed purpose:
        # - Beta1 fetches `/amusa/patchN.html`
        # - release fetches `/amusa/patch/2/am_patchN.html`
        # - both are aliases for the `AM-USA-GAME-PROG` runtime patch transport
        #   consumed by browser parser mode 11 and executed later by `PatchExec`
        # - page number `N` is meaningful: it selects chunk `'1'..'5'` of the
        #   five-piece `0x20000` patch buffer layout.
        for patch_index in sorted(self.patch_pages):
            page = self.patch_pages[patch_index]
            view = _make_static_page_view(tools, page)
            patch_paths = (
                f'/amusa/patch{patch_index}.html',
                f'/amusa/patch/2/am_patch{patch_index}.html',
            )
            for alias_index, patch_path in enumerate(patch_paths):
                app.add_url_rule(
                    patch_path,
                    endpoint=f'{self.name}_patch{patch_index}_{alias_index}',
                    methods=['GET'],
                    view_func=view,
                    host=host,
                )

        upload_view = _make_upload_view(tools)
        app.add_url_rule(
            self.upload_path,
            endpoint=f'{self.name}_upload',
            methods=['GET', 'POST'],
            view_func=upload_view,
            host=host,
        )


def _build_signup_response(
    *,
    username: str,
    password: str,
    signup_service: SqliteSignupService,
) -> Response:
    """Build PS2 signup response payload for a selected username."""

    if not _is_valid_username(username):
        return _error_response('Invalid username.')
    if not _is_valid_password(password):
        return _error_response('Invalid password.')

    result = signup_service.create_or_login(username=username, password=password)
    if not result.ok:
        return _error_response(result.error_message)

    payload = _build_signup_payload(result)
    return Response(payload, mimetype='text/html')


def _is_valid_username(username: str) -> bool:
    """Validate signup username format and length."""

    if not USERNAME_PATTERN.fullmatch(username):
        return False
    if username.startswith('_') or username.endswith('_'):
        return False
    return re.search(r'_{2,}', username) is None


def _is_valid_password(password: str) -> bool:
    """Validate password format and length."""

    encoded_length = len(password.encode('utf-8'))
    if encoded_length < MIN_PASSWORD_LENGTH:
        return False
    if encoded_length > MAX_PASSWORD_LENGTH:
        return False
    return True


def _build_signup_payload(result: SignupResult) -> str:
    """Build successful COMP-SIGNUP payload."""

    # The browser-side client consumes `INPUT-IDS` as a newline-terminated line.
    # Keep the terminator because it is part of the expected protocol payload.
    return (
        '<html>\n'
        '<body>\n'
        'Profile successfully retrieved.<br>\n'
        'Press the Select button and then "End Browser" to save it to the memory card.\n'
        '</body>\n'
        '</html>\n'
        '<!--COMP-SIGNUP-->\n'
        f'<!--INPUT-IDS-->{result.username}\n'
    )


def _error_response(message: str) -> Response:
    """Build generic HTML error response."""

    page = (
        '<html>\n'
        '<body>\n'
        '<h3>Login error</h3>\n'
        f'{message}<br>\n'
        'Please go back and retry.\n'
        '</body>\n'
        '</html>\n'
    )
    return Response(page, mimetype='text/html')
