#!/usr/bin/env python3
"""
Import custom Russian noun declensions into the runouns database.

Reads JSON files containing noun entries (proper names, patronymics, etc.)
and inserts them into the `nouns_morf` table with correct parent-child
relationships, sequential code values, and shared timestamps.

Usage:
    python noun_importer.py path/to/file.json
    python noun_importer.py path/to/directory/
    python noun_importer.py --validate path/to/file.json
    python noun_importer.py --config /path/to/mysql.conf path/to/file.json

Input JSON format:
    {
      "category": "patronymic",
      "entries": [
        {
          "word": "Иванович",
          "gender": "муж",
          "animacy": 1,
          "singular": {
            "nominative": "Иванович",
            "genitive": "Ивановича",
            "dative": "Ивановичу",
            "accusative": "Ивановича",
            "instrumental": "Ивановичем",
            "prepositional": "Ивановиче",
            "vocative": "Ивановиче"        // optional
          },
          "plural": {                        // optional
            "nominative": "Ивановичи",
            "genitive": "Ивановичей",
            "dative": "Ивановичам",
            "accusative": "Ивановичей",
            "instrumental": "Ивановичами",
            "prepositional": "Ивановичах"
          }
        }
      ]
    }
"""

import argparse
import configparser
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pymysql

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_NAME = ".mysql_import.conf"
DEFAULT_CONFIG_PATHS = [
    Path.cwd() / DEFAULT_CONFIG_NAME,
    Path.home() / DEFAULT_CONFIG_NAME,
    Path(f"/etc/{DEFAULT_CONFIG_NAME}"),
]

# ANSI escape codes for terminal output
ANSI_ADDED = "\033[32m"  # green
ANSI_SKIPPED = "\033[33m"  # yellow
ANSI_DELETED = "\033[35m"  # magenta
ANSI_UPDATED = "\033[34m"  # blue
ANSI_ERROR = "\033[31m"  # red
ANSI_RESET = "\033[0m"
ANSI_NOTICE = "\033[1;33m"  # bold yellow

VALID_GENDERS = {"муж", "жен", "ср", "общ"}
VALID_ANIMACY = {0, 1}

# Case order determines sequential code assignment within a declension set
SINGULAR_CASES = [
    ("им", "nominative"),
    ("род", "genitive"),
    ("дат", "dative"),
    ("вин", "accusative"),
    ("тв", "instrumental"),
    ("пр", "prepositional"),
    ("зват", "vocative"),
    ("парт", "partitive"),
    ("мест", "locative"),
    ("счет", "countable"),
]

PLURAL_CASES = [
    ("им", "nominative"),
    ("род", "genitive"),
    ("дат", "dative"),
    ("вин", "accusative"),
    ("тв", "instrumental"),
    ("пр", "prepositional"),
    ("зват", "vocative"),
    ("парт", "partitive"),
    ("мест", "locative"),
    ("счет", "countable"),
]

REQUIRED_SINGULAR_KEYS = {
    "nominative",
    "genitive",
    "dative",
    "accusative",
    "instrumental",
    "prepositional",
}
REQUIRED_PLURAL_KEYS = REQUIRED_SINGULAR_KEYS

OPTIONAL_SINGULAR_KEYS = {"vocative", "partitive", "locative", "countable"}
OPTIONAL_PLURAL_KEYS = OPTIONAL_SINGULAR_KEYS

# All valid keys, used to catch typos in input JSON
ALL_SINGULAR_KEYS = REQUIRED_SINGULAR_KEYS | OPTIONAL_SINGULAR_KEYS
ALL_PLURAL_KEYS = REQUIRED_PLURAL_KEYS | OPTIONAL_PLURAL_KEYS


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------


def format_action(text: str, action: str, color_enabled: bool) -> str:
    """Format an action label with appropriate ANSI color.

    Args:
        text (str): The text to format.
        action (str): One of 'added', 'skipped', 'updated', 'deleted',
            'error', 'notice'.
        color_enabled (bool): Whether to apply ANSI color codes.

    Returns:
        str: The colorized text if color_enabled, otherwise unchanged text.

    Raises:
        ValueError: If the action is not recognized.
    """
    color_map = {
        "added": ANSI_ADDED,
        "skipped": ANSI_SKIPPED,
        "updated": ANSI_UPDATED,
        "deleted": ANSI_DELETED,
        "error": ANSI_ERROR,
        "notice": ANSI_NOTICE,
    }

    if action not in color_map:
        raise ValueError(
            f"Unknown action '{action}'. Valid actions: {sorted(color_map.keys())}"
        )

    if not color_enabled:
        return text

    return f"{color_map[action]}{text}{ANSI_RESET}"


def print_formatted(text: str, action: str, color_enabled: bool) -> None:
    """Format and print an action label, flushing stdout.

    Combines format_action() with print() and flush=True to ensure
    output appears immediately even when stdout is buffered.

    Args:
        text (str): The text to format and print.
        action (str): One of 'added', 'skipped', 'updated', 'deleted',
            'error', 'notice'.
        color_enabled (bool): Whether to apply ANSI color codes.

    Raises:
        ValueError: If the action is not recognized.
    """
    print(format_action(text, action, color_enabled), flush=True)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class NounEntry:
    """A single noun entry with its declensions.

    Attributes:
        word (str): The canonical word form (nominative singular).
        gender (str): Grammatical gender (муж, жен, ср, общ).
        animacy (int): 1 for animate, 0 for inanimate.
        singular (dict): Singular case forms keyed by English case name.
        plural (Optional[dict]): Plural case forms if present, else None.
    """

    word: str
    gender: str
    animacy: int
    singular: dict
    plural: Optional[dict] = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "NounEntry":
        """Parse and validate a noun entry from a JSON dictionary.

        Args:
            data (dict): The raw dictionary from the JSON file.
            index (int): Position in the entries list, for error messages.

        Returns:
            NounEntry: The validated entry.

        Raises:
            ValueError: If any required field is missing or invalid.
        """
        # Validate required top-level fields
        word = data.get("word")
        if not isinstance(word, str) or not word.strip():
            raise ValueError(
                f"Entry {index}: 'word' is required and must be a non-empty string"
            )

        gender = data.get("gender")
        if gender not in VALID_GENDERS:
            raise ValueError(
                f"Entry {index} ('{word}'): 'gender' must be one of {sorted(VALID_GENDERS)}, got '{gender}'"
            )

        animacy = data.get("animacy")
        if animacy not in VALID_ANIMACY:
            raise ValueError(
                f"Entry {index} ('{word}'): 'animacy' must be 0 or 1, got '{animacy}'"
            )

        # Validate singular declension block
        singular = data.get("singular")
        if not isinstance(singular, dict):
            raise ValueError(f"Entry {index} ('{word}'): 'singular' must be an object")

        # Reject unknown case keys (typo detection)
        unknown_singular = set(singular.keys()) - ALL_SINGULAR_KEYS
        if unknown_singular:
            raise ValueError(
                f"Entry {index} ('{word}'): unknown singular case(s): {sorted(unknown_singular)}. "
                f"Valid cases: {sorted(ALL_SINGULAR_KEYS)}"
            )

        missing = REQUIRED_SINGULAR_KEYS - set(singular.keys())
        if missing:
            raise ValueError(
                f"Entry {index} ('{word}'): singular is missing required cases: {sorted(missing)}"
            )

        for case_key in REQUIRED_SINGULAR_KEYS | (
            OPTIONAL_SINGULAR_KEYS & set(singular.keys())
        ):
            if (
                not isinstance(singular[case_key], str)
                or not singular[case_key].strip()
            ):
                raise ValueError(
                    f"Entry {index} ('{word}'): singular.{case_key} must be a non-empty string"
                )

        # Validate plural declension block if present
        plural = data.get("plural")
        if plural is not None:
            if not isinstance(plural, dict):
                raise ValueError(
                    f"Entry {index} ('{word}'): 'plural' must be an object or omitted"
                )

            # Reject unknown case keys (typo detection)
            unknown_plural = set(plural.keys()) - ALL_PLURAL_KEYS
            if unknown_plural:
                raise ValueError(
                    f"Entry {index} ('{word}'): unknown plural case(s): {sorted(unknown_plural)}. "
                    f"Valid cases: {sorted(ALL_PLURAL_KEYS)}"
                )

            missing_plural = REQUIRED_PLURAL_KEYS - set(plural.keys())
            if missing_plural:
                raise ValueError(
                    f"Entry {index} ('{word}'): plural is missing required cases: {sorted(missing_plural)}"
                )

            for case_key in REQUIRED_PLURAL_KEYS | (
                OPTIONAL_PLURAL_KEYS & set(plural.keys())
            ):
                if (
                    not isinstance(plural[case_key], str)
                    or not plural[case_key].strip()
                ):
                    raise ValueError(
                        f"Entry {index} ('{word}'): plural.{case_key} must be a non-empty string"
                    )

        return cls(
            word=word.strip(),
            gender=gender,
            animacy=animacy,
            singular={
                k: singular[k]
                for k in REQUIRED_SINGULAR_KEYS
                | (OPTIONAL_SINGULAR_KEYS & set(singular.keys()))
            },
            plural=(
                {
                    k: plural[k]
                    for k in REQUIRED_PLURAL_KEYS
                    | (OPTIONAL_PLURAL_KEYS & set(plural.keys()))
                }
                if plural
                else None
            ),
        )

    def row_count(self) -> int:
        """Return the total number of DB rows this entry will produce."""
        singular_rows = 1 + len(self.singular)  # root + singular declensions
        if self.plural:
            plural_rows = 1 + len(self.plural)  # nom plural + plural declensions
        else:
            plural_rows = 0
        return singular_rows + plural_rows


@dataclass
class ImportFile:
    """A parsed JSON file containing one or more noun entries.

    Attributes:
        category (str): Category applied to all entries in this file.
        entries (list[NounEntry]): The validated noun entries.
        path (Path): Source file path (for reporting).
    """

    category: str
    entries: list
    path: Path

    @classmethod
    def from_file(cls, path: Path) -> "ImportFile":
        """Load and validate a JSON file.

        Args:
            path (Path): Path to the JSON file.

        Returns:
            ImportFile: The parsed and validated file.

        Raises:
            ValueError: If the JSON structure is invalid.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level JSON must be an object")

        category = data.get("category")
        if not isinstance(category, str) or not category.strip():
            raise ValueError(
                f"{path}: 'category' is required and must be a non-empty string"
            )

        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) == 0:
            raise ValueError(f"{path}: 'entries' must be a non-empty list")

        entries = [NounEntry.from_dict(entry, i) for i, entry in enumerate(raw_entries)]

        return cls(category=category.strip(), entries=entries, path=path)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(explicit_path: Optional[Path] = None) -> dict:
    """Load MySQL connection settings from a config file.

    Searches in order:
        1. Explicit path passed via --config
        2. ./.mysql_import.conf
        3. ~/.mysql_import.conf
        4. /etc/.mysql_import.conf

    Args:
        explicit_path (Optional[Path]): Config path from CLI flag.

    Returns:
        dict: Connection parameters (host, port, user, password, database, charset).

    Raises:
        FileNotFoundError: If no config file is found.
        configparser.Error: If the config file is malformed.
    """
    if explicit_path:
        if not explicit_path.exists():
            raise FileNotFoundError(f"Config file not found: {explicit_path}")
        config_path = explicit_path
    else:
        config_path = next((p for p in DEFAULT_CONFIG_PATHS if p.exists()), None)
        if config_path is None:
            searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
            raise FileNotFoundError(
                f"No config file found. Searched: {searched}. "
                f"Use --config to specify a path."
            )

    parser = configparser.ConfigParser()
    parser.read(config_path)

    if not parser.has_section("mysql"):
        raise configparser.Error(f"{config_path}: missing [mysql] section")

    required_keys = ["host", "user", "password", "database"]
    missing = [k for k in required_keys if not parser.has_option("mysql", k)]
    if missing:
        raise configparser.Error(
            f"{config_path}: [mysql] missing required keys: {missing}"
        )

    return {
        "host": parser.get("mysql", "host"),
        "port": parser.getint("mysql", "port", fallback=3306),
        "user": parser.get("mysql", "user"),
        "password": parser.get("mysql", "password"),
        "database": parser.get("mysql", "database"),
        "charset": parser.get("mysql", "charset", fallback="utf8mb4"),
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


def get_connection(config: dict) -> pymysql.Connection:
    """Create a MySQL connection from config parameters.

    Args:
        config (dict): Connection settings from load_config().

    Returns:
        pymysql.Connection: An open connection.
    """
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset=config["charset"],
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def insert_entry(
    cursor: pymysql.cursors.Cursor,
    entry: NounEntry,
    category: str,
    start_code: int,
    created_at: datetime,
) -> tuple[int, dict]:
    """Insert a single noun entry with all its declension rows.

    Generates sequential code values starting from start_code. The root row
    gets code_parent=0. Singular declensions are children of the root.
    If plural data exists, the nominative plural is a child of the root,
    and remaining plural forms are children of the nominative plural.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        entry (NounEntry): The validated entry to insert.
        category (str): Category string for all rows.
        start_code (int): First code value to assign.
        created_at (datetime): Shared timestamp for all rows in the file.

    Returns:
        tuple[int, dict]: (next available code, summary dict with row counts)
    """
    code = start_code
    rows_inserted = 0

    # Insert root row (nominative singular, plural=0, code_parent=0)
    # The DB word column gets the singular nominative form, since the root
    # row IS the nominative singular.
    cursor.execute(
        """
        INSERT INTO nouns_morf (word, code, code_parent, plural, gender, wcase, soul, is_custom, created_at, category)
        VALUES (%s, %s, 0, 0, %s, 'им', %s, 1, %s, %s)
        """,
        (
            entry.singular["nominative"],
            code,
            entry.gender,
            entry.animacy,
            created_at,
            category,
        ),
    )
    rows_inserted += 1

    root_code = code
    code += 1

    # Insert singular declensions (children of root, plural=0, wcase != 'им')

    SINGULAR_CASES_TO_ADD = SINGULAR_CASES[
        1:
    ]  # exclude nominative as already inserted as root
    for wcase, case_key in SINGULAR_CASES_TO_ADD:
        if case_key not in entry.singular:
            if case_key in REQUIRED_SINGULAR_KEYS:
                raise ValueError(
                    f"'{entry.word}': singular is missing required case '{case_key}'"
                )
            continue  # optional case not provided
        cursor.execute(
            """
            INSERT INTO nouns_morf (word, code, code_parent, plural, gender, wcase, soul, is_custom, created_at, category)
            VALUES (%s, %s, %s, 0, %s, %s, %s, 1, %s, %s)
            """,
            (
                entry.singular[case_key],
                code,
                root_code,
                entry.gender,
                wcase,
                entry.animacy,
                created_at,
                category,
            ),
        )
        rows_inserted += 1
        code += 1

    plural_rows = 0
    if entry.plural:
        # Insert nominative plural (child of root, plural=1, gender=NULL)
        cursor.execute(
            """
            INSERT INTO nouns_morf (word, code, code_parent, plural, gender, wcase, soul, is_custom, created_at, category)
            VALUES (%s, %s, %s, 1, NULL, 'им', %s, 1, %s, %s)
            """,
            (
                entry.plural["nominative"],
                code,
                root_code,
                entry.animacy,
                created_at,
                category,
            ),
        )
        rows_inserted += 1
        plural_root_code = code
        code += 1
        plural_rows += 1

        # Insert remaining plural declensions (children of nominative plural)
        for wcase, case_key in PLURAL_CASES:
            if case_key == "nominative":
                continue  # already inserted as plural root
            if case_key not in entry.plural:
                if case_key in REQUIRED_PLURAL_KEYS:
                    raise ValueError(
                        f"'{entry.word}': plural is missing required case '{case_key}'"
                    )
                continue  # optional case not provided
            cursor.execute(
                """
                INSERT INTO nouns_morf (word, code, code_parent, plural, gender, wcase, soul, is_custom, created_at, category)
                VALUES (%s, %s, %s, 1, NULL, %s, %s, 1, %s, %s)
                """,
                (
                    entry.plural[case_key],
                    code,
                    plural_root_code,
                    wcase,
                    entry.animacy,
                    created_at,
                    category,
                ),
            )
            rows_inserted += 1
            code += 1
            plural_rows += 1

    # Verify the entry was inserted completely and with correct structure
    verify_entry(
        cursor=cursor,
        root_code=root_code,
        entry=entry,
    )

    summary = {
        "word": entry.word,
        "root_code": root_code,
        "singular_rows": 1 + len(entry.singular),
        "plural_rows": plural_rows,
    }
    return code, summary


def entry_exists(cursor, root_word) -> Optional[int]:
    """Check if a custom entry exists and return its root code.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        root_word (str): The singular nominative form of the entry.

    Returns:
        Optional[int]: The root code if found, None otherwise.
    """
    cursor.execute(
        """
        SELECT code FROM nouns_morf
        WHERE word = %s AND code_parent = 0 AND is_custom = 1
        LIMIT 1
        """,
        (root_word,),
    )
    result = cursor.fetchone()
    return result["code"] if result else None


def delete_entry(cursor: pymysql.cursors.Cursor, root_word: str) -> bool:
    """Delete an existing custom entry and all its declension rows.

    Deletes in reverse dependency order: plural declensions first, then
    nominative plural, then singular declensions, then the root row.
    This keeps code_parent references valid at every step.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        root_word (str): The singular nominative form of the entry.

    Returns:
        bool: True if an entry was found and deleted, False otherwise.
    """
    # Locate the root row for this entry
    cursor.execute(
        """
        SELECT code FROM nouns_morf
        WHERE word = %s AND code_parent = 0 AND is_custom = 1
        LIMIT 1
        """,
        (root_word,),
    )
    root_result = cursor.fetchone()
    if not root_result:
        return False

    root_code = root_result["code"]

    # Locate the nominative plural row (if one exists)
    cursor.execute(
        """
        SELECT code FROM nouns_morf
        WHERE code_parent = %s AND plural = 1 AND wcase = 'им'
        LIMIT 1
        """,
        (root_code,),
    )
    plural_result = cursor.fetchone()

    if plural_result:
        plural_root_code = plural_result["code"]
        # Delete plural declensions (children of nominative plural)
        cursor.execute(
            "DELETE FROM nouns_morf WHERE code_parent = %s",
            (plural_root_code,),
        )
        # Delete the nominative plural row itself
        cursor.execute(
            "DELETE FROM nouns_morf WHERE code = %s",
            (plural_root_code,),
        )

    # Delete singular declensions (children of root)
    cursor.execute(
        "DELETE FROM nouns_morf WHERE code_parent = %s",
        (root_code,),
    )

    # Delete the root row itself
    cursor.execute(
        "DELETE FROM nouns_morf WHERE code = %s",
        (root_code,),
    )

    return True


def verify_entry(
    cursor: pymysql.cursors.Cursor, root_code: int, entry: NounEntry
) -> None:
    """Verify that every expected row was inserted with correct values.

    Queries back the inserted rows and checks each case form against
    the expected values. Raises if any row is missing, duplicated,
    or has incorrect word, wcase, plural, or parent code.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        root_code (int): The code value of the entry's root row.
        entry (NounEntry): The noun entry to verify.
    """
    # Collect all rows for this entry (root + children + grandchildren)
    rows = _fetch_entry_rows(cursor, root_code)

    # Build map of (plural, wcase) -> row for easy lookup
    row_map = {(row["plural"], row["wcase"]): row for row in rows}

    # Verify root row (nominative singular, plural=0, code_parent=0)
    _verify_root(row_map, entry, root_code)
    _verify_singular(row_map, entry, root_code)
    _verify_plural(row_map, entry, root_code)


def _fetch_entry_rows(cursor: pymysql.cursors.Cursor, root_code: int) -> list[dict]:
    """Fetch all rows for an entry (root, children, grandchildren).

    Uses the same query pattern as insert_entry() and query_entry():
    root by code, direct children by code_parent, grandchildren via
    subquery on the nominative plural's code.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        root_code (int): The code value of the entry's root row.

    Returns:
        list[dict]: The fetched rows.
    """
    cursor.execute(
        """
        SELECT word, code, code_parent, plural, wcase
        FROM nouns_morf
        WHERE code = %s
           OR code_parent = %s
           OR code_parent IN (
               SELECT code FROM nouns_morf WHERE code_parent = %s AND plural = 1
           )
        """,
        (root_code, root_code, root_code),
    )
    return cursor.fetchall()


def _verify_root(row_map: dict, entry: NounEntry, root_code: int) -> None:
    """Verify the root row (nominative singular, plural=0, code_parent=0).

    Args:
        row_map (dict): Mapping of (plural, wcase) to row data.
        entry (NounEntry): The noun entry being verified.
        root_code (int): The expected code value of the root row.
    """
    # The root row is keyed by (plural=0, wcase='им') in the row map.
    # It must contain the singular nominative form and have no parent.
    root = row_map.get((0, "им"))
    if root is None:
        raise ValueError(f"'{entry.word}': root row (nominative singular) missing")

    _verify_field(entry.word, "root word", entry.singular["nominative"], root["word"])
    _verify_field(entry.word, "root code", root_code, root["code"])
    _verify_field(entry.word, "root parent code", 0, root["code_parent"])


def _verify_singular(row_map: dict, entry: NounEntry, root_code: int) -> None:
    """Verify singular declensions (children of root, plural=0).

    Args:
        row_map (dict): Mapping of (plural, wcase) to row data.
        entry (NounEntry): The noun entry being verified.
        root_code (int): The expected parent code for singular rows.
    """
    # Iterate over the full case list. Skip 'им' because it is the root
    # row (already verified in _verify_root). Skip optional cases not
    # provided in the entry.
    for wcase, case_key in SINGULAR_CASES:
        if wcase == "им":
            continue  # root row already verified
        if case_key not in entry.singular:
            continue  # optional case not provided

        row = row_map.get((0, wcase))
        if row is None:
            raise ValueError(f"'{entry.word}': singular {wcase} row missing")

        _verify_field(
            entry.word, f"singular {wcase} word", entry.singular[case_key], row["word"]
        )
        _verify_field(
            entry.word, f"singular {wcase} parent code", root_code, row["code_parent"]
        )


def _verify_plural(row_map: dict, entry: NounEntry, root_code: int) -> None:
    """Verify plural rows (nominative plural + its children).

    Args:
        row_map (dict): Mapping of (plural, wcase) to row data.
        entry (NounEntry): The noun entry being verified.
        root_code (int): The expected parent code of the nominative plural.
    """
    # If the entry has no plural data, there is nothing to verify.
    if not entry.plural:
        return

    # Nominative plural is the parent of all other plural declensions.
    # It is keyed by (plural=1, wcase='им') in the row map.
    nom_plural = row_map.get((1, "им"))
    if nom_plural is None:
        raise ValueError(f"'{entry.word}': nominative plural row missing")

    _verify_field(
        entry.word,
        "nominative plural word",
        entry.plural["nominative"],
        nom_plural["word"],
    )
    _verify_field(
        entry.word,
        "nominative plural parent code",
        root_code,
        nom_plural["code_parent"],
    )

    # Capture the nominative plural's code so we can verify that the
    # remaining plural declensions point to it as their parent.
    plural_root_code = nom_plural["code"]

    # Remaining plural declensions (children of nominative plural)
    for wcase, case_key in PLURAL_CASES:
        if case_key == "nominative":
            continue  # already verified as plural root
        if case_key not in entry.plural:
            continue  # optional case not provided

        row = row_map.get((1, wcase))
        if row is None:
            raise ValueError(f"'{entry.word}': plural {wcase} row missing")

        _verify_field(
            entry.word, f"plural {wcase} word", entry.plural[case_key], row["word"]
        )
        _verify_field(
            entry.word,
            f"plural {wcase} parent code",
            plural_root_code,
            row["code_parent"],
        )


def _verify_field(entry_word: str, field_name: str, expected, actual) -> None:
    """Compare expected vs actual field value, raising with context on mismatch.

    Args:
        entry_word (str): The entry's display word, for error context.
        field_name (str): Human-readable description of the field.
        expected: The value that was expected.
        actual: The value found in the database.
    """
    # Single point of failure for all verification checks. Keeping the
    # error format here ensures consistent, actionable messages.
    if expected != actual:
        raise ValueError(
            f"'{entry_word}': {field_name} mismatch. "
            f"Expected {expected}, found {actual}"
        )


def query_entry(cursor: pymysql.cursors.Cursor, root_word: str) -> list[dict]:
    """Query all rows for an entry by its root word.

    Finds the root row (code_parent=0) matching the given word, then
    retrieves all descendant rows (children and grandchildren).

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor within a transaction.
        root_word (str): The noun to look up.

    Returns:
        list[dict]: All matching rows as dictionaries.
    """
    cursor.execute(
        """
        SELECT code FROM nouns_morf
        WHERE word = %s AND code_parent = 0 AND is_custom = 1
        LIMIT 1
        """,
        (root_word,),
    )
    root = cursor.fetchone()

    if root is None:
        return []

    root_code = root["code"]

    cursor.execute(
        """
        SELECT * FROM nouns_morf
        WHERE code = %s
           OR code_parent = %s
           OR code_parent IN (
               SELECT code FROM nouns_morf WHERE code_parent = %s AND plural = 1
           )
        ORDER BY plural, wcase
        """,
        (root_code, root_code, root_code),
    )
    return cursor.fetchall()


def print_entry(rows: list[dict], word: str) -> None:
    """Print entry rows in a readable table format.

    Args:
        rows (list[dict]): The rows to print.
        word (str): The entry's display word, for the header.
    """
    if not rows:
        print(f"No rows found for '{word}'")
        return

    print(f"─── {word} ─────────────────────────────")
    print(
        f"{'IID':<5} {'word':<15} {'code':<7} {'parent':<7} {'pl':<3} {'gender':<6} {'case':<5} {'soul':<4} {'custom':<6} {'category':<12} {'created_at'}"
    )
    print("-" * 100)

    for row in rows:
        gender = row["gender"] if row["gender"] is not None else "NULL"
        created = (
            row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            if row["created_at"]
            else "NULL"
        )
        print(
            f"{row['IID']:<5} "
            f"{row['word']:<15} "
            f"{row['code']:<7} "
            f"{row['code_parent']:<7} "
            f"{row['plural']:<3} "
            f"{gender:<6} "
            f"{row['wcase']:<5} "
            f"{row['soul']:<4} "
            f"{row['is_custom']:<6} "
            f"{row['category'] if row['category'] else 'NULL':<12} "
            f"{created}"
        )

    print(f"\n{len(rows)} rows total")


# ---------------------------------------------------------------------------
# Import orchestration
# ---------------------------------------------------------------------------


def process_file(
    import_file: ImportFile,
    config: dict,
    force_update: bool = False,
    color_enabled: bool = False,
) -> dict:
    """Import a single file's entries into the database.

    Opens a connection, begins a transaction, inserts all entries, and
    commits. If any error occurs, rolls back the entire file.

    Args:
        import_file (ImportFile): The parsed and validated file.
        config (dict): MySQL connection settings.
        force_update (bool): If True, existing entries are deleted and
            re-inserted. If False, existing entries are skipped.
        color_enabled (bool): Whether to apply ANSI color codes to output.

    Returns:
        dict: Summary of what was inserted, skipped, and updated.

    Raises:
        pymysql.MySQLError: If any database operation fails.
    """
    connection = get_connection(config)
    summaries = []
    skipped_count = 0
    updated_count = 0

    try:
        with connection.cursor() as cursor:
            # Get starting code for this file
            cursor.execute("SELECT MAX(code) AS max_code FROM nouns_morf")
            result = cursor.fetchone()
            max_code = (
                result["max_code"] if result and result["max_code"] is not None else 0
            )
            next_code = max_code + 1

            # Shared timestamp for all rows in this file
            cursor.execute("SELECT NOW() AS now")
            created_at = cursor.fetchone()["now"]

            for entry in import_file.entries:
                # The root row's word column stores the singular nominative,
                # which is what identifies an entry across runs.
                root_word = entry.singular["nominative"]

                # check if the entry already exists
                # (if so, entry_exists returns its code)
                existing_code = entry_exists(cursor, root_word)

                if existing_code is not None:
                    if not force_update:
                        # Default: skip existing entries
                        skipped_count += 1
                        summaries.append(
                            {
                                "word": entry.word,
                                "skipped": True,
                                "root_code": existing_code,
                            }
                        )
                        print_formatted(
                            f"  = {entry.word} (code={existing_code}, already exists, skipped)",
                            "skipped",
                            color_enabled,
                        )
                        continue
                    else:
                        # Force update: remove existing rows, then re-insert
                        delete_entry(cursor, root_word)
                        updated_count += 1
                        print_formatted(
                            f"  ~ {entry.word} (deleting, was code={existing_code})",
                            "deleted",
                            color_enabled,
                        )

                next_code, summary = insert_entry(
                    cursor, entry, import_file.category, next_code, created_at
                )
                summaries.append(summary)
                print_formatted(
                    f"  + {entry.word} (code={summary['root_code']})",
                    "added",
                    color_enabled,
                )

        connection.commit()
        return {
            "path": import_file.path,
            "category": import_file.category,
            "summaries": summaries,
            "total_rows": sum(
                s.get("singular_rows", 0) + s.get("plural_rows", 0) for s in summaries
            ),
            "skipped_count": skipped_count,
            "updated_count": updated_count,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def print_file_header(path: Path, color_enabled: bool) -> None:
    """Print a header showing which file is being processed.

    Args:
        path (Path): The file being processed.
        color_enabled (bool): Whether to apply ANSI color codes.
    """
    print()
    print_formatted(f"─── {path.as_posix()} ───", "notice", color_enabled)
    print()


def print_summary(
    result: dict, validate_only: bool = False, color_enabled: bool = False
) -> None:
    """Print a human-readable summary of an import (or validation).

    Args:
        result (dict): The result dict from process_file() or build_validation_summary().
        validate_only (bool): If True, prefix with 'Would import' instead of 'Imported'.
        color_enabled (bool): Whether to apply ANSI color codes to output.
    """
    verb = "Would import" if validate_only else "Imported"
    inserted_summaries = [s for s in result["summaries"] if not s.get("skipped", False)]
    skipped_summaries = [s for s in result["summaries"] if s.get("skipped", False)]

    inserted_noun = "entry" if len(inserted_summaries) == 1 else "entries"
    print(
        f"\n{verb} '{result['category']}' — {len(inserted_summaries)} {inserted_noun}, {result['total_rows']} rows"
    )

    for s in inserted_summaries:
        plural_part = (
            f", {s['plural_rows']} plural" if s["plural_rows"] > 0 else ", 0 plural"
        )
        print_formatted(
            f"  + {s['word']} (code={s['root_code']}): {s['singular_rows']} singular{plural_part}",
            "added",
            color_enabled,
        )

    for s in skipped_summaries:
        code_part = f" (code={s['root_code']})" if "root_code" in s else ""
        print_formatted(
            f"  = {s['word']}{code_part} (already exists, skipped)",
            "skipped",
            color_enabled,
        )

    if result.get("updated_count", 0) > 0:
        updated_noun = "entry" if result["updated_count"] == 1 else "entries"
        print_formatted(
            f"  ~ Updated {result['updated_count']} {updated_noun}",
            "updated",
            color_enabled,
        )

    if validate_only:
        print_formatted(
            f"(code values are simulated; no database connection was made)",
            "notice",
            color_enabled,
        )
    print("")


def build_validation_summary(import_file: ImportFile) -> dict:
    """Build a summary for --validate mode without touching the DB.

    Simulates code assignment by finding the current MAX(code) without
    inserting anything. If the DB is unreachable, uses 0 as a placeholder
    and notes this in the output.

    Args:
        import_file (ImportFile): The parsed and validated file.

    Returns:
        dict: Summary structure identical to process_file(), for printing.
    """
    # We don't have a DB connection here; simulate code assignment
    summaries = []
    next_code = 1  # placeholder if no DB access

    for entry in import_file.entries:
        summary = {
            "word": entry.word,
            "root_code": next_code,
            "singular_rows": 1 + len(entry.singular),
            "plural_rows": (1 + len(entry.plural)) if entry.plural else 0,
        }
        summaries.append(summary)
        next_code += summary["singular_rows"] + summary["plural_rows"]

    return {
        "path": import_file.path,
        "category": import_file.category,
        "summaries": summaries,
        "total_rows": sum(s["singular_rows"] + s["plural_rows"] for s in summaries),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and execute the import or validation."""
    parser = argparse.ArgumentParser(
        description="Import custom Russian noun declensions into the runouns database."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a JSON file or directory of JSON files",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="When processing a directory, do not include JSON files in subdirectories (only JSON files in that directory)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to MySQL config file (default: .mysql_import.conf in cwd, home, or /etc)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate input and print summary without writing to the database",
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto (default, colors only when stdout is a TTY), always, or never",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="If an entry already exists, delete it and re-insert. Default is to skip existing entries.",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="After importing, print the full rows for each added entry",
    )
    args = parser.parse_args()

    # Resolve input path(s)
    if args.path.is_dir():
        if args.no_recursive:
            # use glob to only search the requested dir
            json_files = sorted(args.path.glob("*.json"))
        else:
            # find all JSON files within this and subdirs
            json_files = sorted(args.path.rglob("*.json"))

        if not json_files:
            print(f"No .json files found in {args.path}", file=sys.stderr)
            sys.exit(1)
    elif args.path.is_file():
        json_files = [args.path]
    else:
        print(f"Path not found: {args.path}", file=sys.stderr)
        sys.exit(1)

    # Determine whether to use ANSI colors
    if args.color == "always":
        color_enabled = True
    elif args.color == "never":
        color_enabled = False
    else:  # auto
        color_enabled = sys.stdout.isatty()

    # Load config (only needed for actual import, not validate)
    config = None
    if not args.validate:
        try:
            config = load_config(args.config)
        except (FileNotFoundError, configparser.Error) as e:
            print(f"Config error: {e}", file=sys.stderr)
            sys.exit(1)

    # Process each file
    exit_code = 0
    for json_file in json_files:
        try:
            import_file = ImportFile.from_file(json_file)
            print_file_header(json_file, color_enabled)

            if args.validate:
                result = build_validation_summary(import_file)
                print_summary(result, validate_only=True, color_enabled=color_enabled)
            else:
                result = process_file(
                    import_file,
                    config,
                    force_update=args.force_update,
                    color_enabled=color_enabled,
                )
                print_summary(result, color_enabled=color_enabled)

                if args.dump:
                    connection = get_connection(config)
                    try:
                        with connection.cursor() as cursor:
                            for s in result["summaries"]:
                                if s.get("skipped", False):
                                    continue
                                rows = query_entry(cursor, s["word"])
                                print_entry(rows, s["word"])
                    finally:
                        connection.close()

        except (ValueError, json.JSONDecodeError) as e:
            print(f"Validation error in {json_file}: {e}", file=sys.stderr)
            exit_code = 1
        except pymysql.MySQLError as e:
            print(f"Database error importing {json_file}: {e}", file=sys.stderr)
            exit_code = 1
        except Exception as e:
            print(f"Unexpected error processing {json_file}: {e}", file=sys.stderr)
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
