#!/usr/bin/env python3
"""
Manage the runouns database.

Maintains custom Russian noun declensions (proper names, patronymics,
surnames, etc.) that are not present in the original Sshra dump, and
provides utility operations for inspecting and verifying the database.

Operations are grouped into two command sets:

    file - File-based operations on JSON entries:
        add           - Insert entries from JSON, skipping existing
        update        - Replace existing entries with fresh data
        delete        - Remove entries listed in JSON (with confirmation)
        validate-json - Validate JSON structure without touching the DB
        verify        - Check DB entries against a JSON file
        verify-all    - verify plus full DB sanity checks

    util - Standalone operations:
        --word WORD   - Query and display a single entry by word
        --sanity      - Run DB sanity checks (indexes, columns, data fixes)

Usage:
    python manager.py file add path/to/file.json
    python manager.py file add path/to/directory/
    python manager.py file update path/to/file.json
    python manager.py file delete path/to/file.json
    python manager.py file verify path/to/file.json
    python manager.py file verify-all path/to/file.json
    python manager.py file validate-json path/to/file.json
    python manager.py util --word Иван
    python manager.py util --sanity

Input JSON format:
    {
      "category": "name",
      "entries": [
        {
          "word": "Иван",
          "gender": "муж",
          "animacy": 1,
          "singular": {
            "nominative": "Иван",
            "genitive": "Ивана",
            "dative": "Ивану",
            "accusative": "Ивана",
            "instrumental": "Иваном",
            "prepositional": "Иване",
            "vocative": "Иване"
          },
          "plural": {
            "nominative": "Иваны",
            "genitive": "Иванов",
            "dative": "Иванам",
            "accusative": "Иванов",
            "instrumental": "Иванами",
            "prepositional": "Иванах"
          }
        }
      ]
    }

Indeclinable entries (no declension forms) are supported via an
"indeclinable" flag:

    {
      "word": "Бондаренко",
      "gender": "общ",
      "animacy": 1,
      "indeclinable": true
    }
"""

import argparse
import configparser
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

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

VALID_GENDERS = {"муж", "жен", "ср", "общ"}
VALID_ANIMACY = {0, 1}

# Case order determines sequential code assignment within a declension set.
# The first six are the standard Russian cases; the remaining four are rare
# or archaic forms that may not be present for every noun.
CASE_ORDER = [
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

REQUIRED_CASE_KEYS = {
    "nominative",
    "genitive",
    "dative",
    "accusative",
    "instrumental",
    "prepositional",
}
OPTIONAL_CASE_KEYS = {"vocative", "partitive", "locative", "countable"}
ALL_CASE_KEYS = REQUIRED_CASE_KEYS | OPTIONAL_CASE_KEYS

# ANSI color codes
ANSI_ADDED = "\033[32m"  # green
ANSI_SKIPPED = "\033[33m"  # yellow
ANSI_DELETED = "\033[35m"  # magenta
ANSI_UPDATED = "\033[34m"  # blue
ANSI_ERROR = "\033[31m"  # red
ANSI_NOTICE = "\033[1;33m"  # bold yellow
ANSI_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


class Logger:
    """Handles all terminal output with color and routing.

    Routes errors to stderr by default; all other output goes to
    stdout. The `use_stderr` flag can force all output to stderr.

    The color mapping is centralized here so call sites refer to
    semantic action names ("added", "skipped", etc.) rather than
    specific ANSI codes.

    Attributes:
        color_enabled (bool): Whether to apply ANSI color codes.
        use_stderr (bool): If True, route all output to stderr.
    """

    #: Maps action names to their ANSI escape sequence
    COLOR_MAP = {
        "added": ANSI_ADDED,
        "skipped": ANSI_SKIPPED,
        "deleted": ANSI_DELETED,
        "updated": ANSI_UPDATED,
        "error": ANSI_ERROR,
        "notice": ANSI_NOTICE,
        "plain": "",
    }

    def __init__(self, color_enabled: bool = False, use_stderr: bool = False) -> None:
        """Initialize the logger.

        Args:
            color_enabled (bool): Whether to apply ANSI color codes.
            use_stderr (bool): If True, route all output to stderr.
        """
        self.color_enabled = color_enabled
        self.use_stderr = use_stderr

    def _format(self, text: str, action: str) -> str:
        """Apply ANSI color if enabled.

        Args:
            text (str): The text to format.
            action (str): The action name (must be in COLOR_MAP).

        Returns:
            str: The formatted text.

        Raises:
            ValueError: If the action is not recognized.
        """
        if action not in self.COLOR_MAP:
            raise ValueError(
                f"Unknown action '{action}'. Valid actions: {sorted(self.COLOR_MAP.keys())}"
            )
        if not self.color_enabled or action == "plain":
            return text
        return f"{self.COLOR_MAP[action]}{text}{ANSI_RESET}"

    def _print(self, text: str, action: str) -> None:
        """Route and print formatted text.

        Args:
            text (str): The text to print.
            action (str): The action name (must be in COLOR_MAP).
        """
        target = sys.stderr if (self.use_stderr or action == "error") else sys.stdout
        print(self._format(text, action), flush=True, file=target)

    # Public methods — one per action type

    def info(self, text: str) -> None:
        """Print plain informational text to stdout."""
        self._print(text, "plain")

    def added(self, text: str) -> None:
        """Print an 'added' action line."""
        self._print(text, "added")

    def skipped(self, text: str) -> None:
        """Print a 'skipped' action line."""
        self._print(text, "skipped")

    def updated(self, text: str) -> None:
        """Print an 'updated' action line."""
        self._print(text, "updated")

    def deleted(self, text: str) -> None:
        """Print a 'deleted' action line."""
        self._print(text, "deleted")

    def error(self, text: str) -> None:
        """Print an error line to stderr."""
        self._print(text, "error")

    def notice(self, text: str) -> None:
        """Print a high-visibility notice line."""
        self._print(text, "notice")

    def table(self, rows: list[dict], word: Optional[str] = None) -> None:
        """Print a data table of entry rows.

        Args:
            rows (list[dict]): The rows to print (each row is a dict
                with keys matching the nouns_morf columns).
            word (Optional[str]): If provided, prints a header with
                the word above the table.
        """
        if word:
            self.info(f"─── {word} ─────────────────────────────")

        header = (
            f"{'IID':<5} {'word':<15} {'code':<7} {'parent':<7} {'pl':<3} "
            f"{'gender':<6} {'case':<5} {'soul':<4} {'custom':<6} "
            f"{'category':<12} {'created_at'}"
        )
        self.info(header)
        self.info("-" * 100)

        for row in rows:
            gender = row["gender"] if row["gender"] is not None else "NULL"
            wcase = row["wcase"] if row["wcase"] is not None else "NULL"
            created = (
                row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                if row["created_at"]
                else "NULL"
            )
            self.info(
                f"{row['IID']:<5} "
                f"{row['word']:<15} "
                f"{row['code']:<7} "
                f"{row['code_parent']:<7} "
                f"{row['plural']:<3} "
                f"{gender:<6} "
                f"{wcase:<5} "
                f"{row['soul']:<4} "
                f"{row['is_custom']:<6} "
                f"{row['category'] if row['category'] else 'NULL':<12} "
                f"{created}"
            )

        self.info(f"\n{len(rows)} rows total")


#: Module-level default logger for direct use by module importers.
#: Callers that want color or stderr routing can create their own
#: Logger instance.
logger = Logger()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class NounEntry:
    """A single noun entry with its declensions.

    Attributes:
        word (str): The display word (nominative singular for
            declinable entries; the invariant form for indeclinable).
        gender (str): Grammatical gender (муж, жен, ср, общ).
        animacy (int): 1 for animate, 0 for inanimate.
        singular (dict): Singular case forms keyed by English case name.
        plural (Optional[dict]): Plural case forms if present, else None.
        indeclinable (bool): True if the noun has no declensions
            (root row only, wcase = NULL).
    """

    word: str
    gender: str
    animacy: int
    singular: dict
    plural: Optional[dict] = None
    indeclinable: bool = False

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
        word = cls._validate_word(data, index)
        gender = cls._validate_gender(data, index, word)
        animacy = cls._validate_animacy(data, index, word)
        indeclinable = cls._validate_indeclinable(data, index, word)

        if indeclinable:
            singular, plural = cls._validate_indeclinable_blocks(data, index, word)
        else:
            singular = cls._validate_declension_block(data, index, word, "singular")
            plural = cls._validate_declension_block(
                data, index, word, "plural", optional=True
            )

        return cls(
            word=word,
            gender=gender,
            animacy=animacy,
            singular=singular,
            plural=plural,
            indeclinable=indeclinable,
        )

    @staticmethod
    def _validate_word(data: dict, index: int) -> str:
        """Validate and return the 'word' field."""
        word = data.get("word")
        if not isinstance(word, str) or not word.strip():
            raise ValueError(
                f"Entry {index}: 'word' is required and must be a non-empty string"
            )
        return word.strip()

    @staticmethod
    def _validate_gender(data: dict, index: int, word: str) -> str:
        """Validate and return the 'gender' field."""
        gender = data.get("gender")
        if gender not in VALID_GENDERS:
            raise ValueError(
                f"Entry {index} ('{word}'): 'gender' must be one of "
                f"{sorted(VALID_GENDERS)}, got '{gender}'"
            )
        return gender

    @staticmethod
    def _validate_animacy(data: dict, index: int, word: str) -> int:
        """Validate and return the 'animacy' field."""
        animacy = data.get("animacy")
        if animacy not in VALID_ANIMACY:
            raise ValueError(
                f"Entry {index} ('{word}'): 'animacy' must be 0 or 1, got '{animacy}'"
            )
        return animacy

    @staticmethod
    def _validate_indeclinable(data: dict, index: int, word: str) -> bool:
        """Validate and return the 'indeclinable' flag."""
        indeclinable = data.get("indeclinable", False)
        if not isinstance(indeclinable, bool):
            raise ValueError(
                f"Entry {index} ('{word}'): 'indeclinable' must be a boolean"
            )
        return indeclinable

    @staticmethod
    def _validate_indeclinable_blocks(
        data: dict, index: int, word: str
    ) -> tuple[dict, None]:
        """Validate that indeclinable entries have no declension blocks.

        Returns:
            tuple: (empty singular dict, None for plural)
        """
        if data.get("singular") not in (None, {}):
            raise ValueError(
                f"Entry {index} ('{word}'): indeclinable entries must not "
                f"have 'singular' cases"
            )
        if data.get("plural") is not None:
            raise ValueError(
                f"Entry {index} ('{word}'): indeclinable entries must not "
                f"have 'plural'"
            )
        return {}, None

    @staticmethod
    def _validate_declension_block(
        data: dict,
        index: int,
        word: str,
        block_name: str,
        optional: bool = False,
    ) -> dict:
        """Validate a declension block ('singular' or 'plural').

        Args:
            data (dict): The raw entry dictionary.
            index (int): Entry position, for error messages.
            word (str): The entry's display word.
            block_name (str): 'singular' or 'plural'.
            optional (bool): If True, the block may be absent.

        Returns:
            dict: The validated case forms (empty dict if optional
                and absent).

        Raises:
            ValueError: If the block is invalid.
        """
        block = data.get(block_name)

        if block is None:
            if optional:
                return {}
            raise ValueError(
                f"Entry {index} ('{word}'): '{block_name}' must be an object"
            )

        if not isinstance(block, dict):
            raise ValueError(
                f"Entry {index} ('{word}'): '{block_name}' must be an object"
            )

        # Reject unknown keys (typo detection)
        unknown = set(block.keys()) - ALL_CASE_KEYS
        if unknown:
            raise ValueError(
                f"Entry {index} ('{word}'): unknown {block_name} case(s): "
                f"{sorted(unknown)}. Valid cases: {sorted(ALL_CASE_KEYS)}"
            )

        # Required cases must be present
        missing = REQUIRED_CASE_KEYS - set(block.keys())
        if missing:
            raise ValueError(
                f"Entry {index} ('{word}'): {block_name} is missing "
                f"required cases: {sorted(missing)}"
            )

        # Validate and strip each value
        clean = {}
        for case_key in block:
            if not isinstance(block[case_key], str) or not block[case_key].strip():
                raise ValueError(
                    f"Entry {index} ('{word}'): {block_name}.{case_key} "
                    f"must be a non-empty string"
                )
            clean[case_key] = block[case_key].strip()

        return clean

    def row_count(self) -> int:
        """Return the total number of DB rows this entry produces."""
        if self.indeclinable:
            return 1
        count = 1 + len(self.singular)  # root + singular declensions
        if self.plural:
            count += 1 + len(self.plural)  # nom plural + plural declensions
        return count

    def root_word(self) -> str:
        """Return the word stored in the root row's word column.

        For declinable entries, this is the singular nominative form.
        For indeclinable entries, this is the entry's word field.
        """
        if self.indeclinable:
            return self.word
        return self.singular["nominative"]


@dataclass
class ImportFile:
    """A parsed JSON file containing one or more noun entries.

    Attributes:
        category (str): Category applied to all entries in this file.
        entries (list[NounEntry]): The validated noun entries.
        path (Path): Source file path.
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


@dataclass
class EntryResult:
    """The outcome of a single entry within an operation.

    Attributes:
        word (str): The entry's display word.
        root_code (int): The root code in the database (if applicable).
        status (str): One of 'added', 'updated', 'deleted',
            'skipped', or 'not_found'.
        rows_affected (int): Number of rows inserted or deleted.
        detail (str): Optional contextual detail for the summary.
    """

    word: str
    root_code: int
    status: str
    rows_affected: int
    detail: str = ""
    error: bool = False
    error_message: str = ""


@dataclass
class FileResult:
    """The outcome of an operation on a single file.

    Attributes:
        path (Path): Source file path.
        category (str): Category from the file.
        operation (str): The operation that produced this result
            (e.g. 'add', 'update', 'delete', 'verify').
        entries (list[EntryResult]): Per-entry outcomes.
        total_rows (int): Total rows affected (inserted or deleted).
        cancelled (bool): True if the user cancelled the operation.
        notice (str): Optional addendum displayed by print_file_summary().
    """

    path: Path
    category: str
    operation: str = ""
    entries: list = field(default_factory=list)
    total_rows: int = 0
    cancelled: bool = False
    notice: str = ""


@dataclass
class ErrorResult:
    """Holds error details for a job error.

    Attributes:
        file (FileResult): The FileResult this error originated from,
            or None for general errors not tied to a specific file.
        entry (EntryResult): The EntryResult this error originated
            from, or None for errors not tied to a specific entry.
        message (str): The error message to display.
        general (bool): True if this is a general error not tied to
            a specific file or entry.
    """

    file: FileResult = None
    entry: EntryResult = None
    message: str = ""
    general: bool = False


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
        dict: Connection parameters.

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
# Database layer
# ---------------------------------------------------------------------------


@contextmanager
def db_transaction(config: dict) -> Generator[pymysql.cursors.Cursor, None, None]:
    """Provide a cursor within a transaction.

    Handles connection setup, commit on success, rollback on error,
    and connection cleanup.

    Args:
        config (dict): MySQL connection settings.

    Yields:
        pymysql.cursors.Cursor: The active cursor.

    Raises:
        pymysql.MySQLError: If any database operation fails.
    """
    connection = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset=config["charset"],
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        with connection.cursor() as cursor:
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_next_code(cursor: pymysql.cursors.Cursor) -> int:
    """Return the next available code value for new entries.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.

    Returns:
        int: MAX(code) + 1, or 1 if the table is empty.
    """
    cursor.execute("SELECT MAX(code) AS max_code FROM nouns_morf")
    result = cursor.fetchone()
    return (result["max_code"] + 1) if result and result["max_code"] is not None else 1


def get_created_at(cursor: pymysql.cursors.Cursor) -> datetime:
    """Return the current database timestamp for shared use across a file.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.

    Returns:
        datetime: The database's NOW() value.
    """
    cursor.execute("SELECT NOW() AS now")
    return cursor.fetchone()["now"]


def entry_exists(
    cursor: pymysql.cursors.Cursor,
    root_word: str,
    case_insensitive: bool = False,
) -> Optional[int]:
    """Return the root code if a custom entry exists, else None.

    Uses binary collation for ё/е distinction. If case_insensitive
    is True, lowercases both the stored value and the lookup word
    before comparison, so иван matches Иван while Петр and Пётр
    remain distinct.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        root_word (str): The root row's word column value.
        case_insensitive (bool): If True, match case-insensitively.

    Returns:
        Optional[int]: The root code if found, None otherwise.
    """

    # ============ Build WHERE predicate ================
    #
    # Case-insensitive WHERE predicate:
    #
    #     WHERE LOWER(word) = %s COLLATE utf8mb4_bin
    #
    # Case-sensitive WHERE predicate:
    #
    #     WHERE word = %s COLLATE utf8mb4_bin
    #
    # ===================================================
    #
    # Notes:
    # 1. Lowercase both column and parameter (%s value passed to
    #    cursor.execute) so they match
    # 2. The "COLLATE utf8mb4_bin" is required and must go on the right
    #    side of the predicate. Explanation:
    #   - Default string comparison in MySQL doesn't distinguish
    #     between е and ё.
    #   - You can specify your own comparison via COLLATE keyword.
    #   - utf8mb4_bin is one COLLATE type. It compares raw bytes,
    #     making е and ё distinct.
    #   - We want to distinguish between е and ё, so we specify
    #     COLLATE utf8mb4_bin.
    #   - The COLLATE keyword must be applied on either the column
    #     (e.g., word) or parameter (e.g., the %s).
    #   - The columns in this database table are utf8mb3, and MySQL
    #     doesn't allow COLLATE utf8mb4_bin on such values, so for
    #     our purposes the COLLATE must be applied on the parameter.
    #     (Parameters here are just strings, they have no predefined
    #     column charset, so nothing for MySQL to reject.)
    if case_insensitive:
        where_clause = "LOWER(word) = %s COLLATE utf8mb4_bin"
        param = root_word.lower()
    else:
        where_clause = "word = %s COLLATE utf8mb4_bin"
        param = root_word

    cursor.execute(
        f"""
        SELECT code FROM nouns_morf
        WHERE {where_clause}
          AND code_parent = 0
        LIMIT 1
        """,
        (param,),
    )
    result = cursor.fetchone()
    return result["code"] if result else None


def insert_entry(
    cursor: pymysql.cursors.Cursor,
    entry: NounEntry,
    category: str,
    start_code: int,
    created_at: datetime,
) -> int:
    """Insert a single noun entry and return the next available code.

    Handles both declinable and indeclinable entries. For declinable
    entries, inserts root, singular declensions, nominative plural,
    and plural declensions with correct parent-child wiring. For
    indeclinable entries, inserts a single root row with wcase = NULL.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        entry (NounEntry): The validated entry to insert.
        category (str): Category string for all rows.
        start_code (int): First code value to assign.
        created_at (datetime): Shared timestamp for all rows.

    Returns:
        int: code assigned to root row of the new entry
    """
    code = start_code

    # Insert the root row. Indeclinable entries store wcase = NULL;
    # declinable entries store the singular nominative with wcase = 'им'.
    if entry.indeclinable:
        root_word = entry.word
        root_wcase = None
    else:
        root_word = entry.singular["nominative"]
        root_wcase = "им"

    cursor.execute(
        """
        INSERT INTO nouns_morf
            (word, code, code_parent, plural, gender, wcase, soul,
             is_custom, created_at, category)
        VALUES
            (%s, %s, 0, 0, %s, %s, %s, 1, %s, %s)
        """,
        (
            root_word,
            code,
            entry.gender,
            root_wcase,
            entry.animacy,
            created_at,
            category,
        ),
    )
    root_code = code  # the root row's code is the first code assigned
    code += 1

    if entry.indeclinable:
        verify_entry(cursor, root_code, entry)
        return root_code

    # Insert singular declensions (children of root, plural=0).
    # Skip nominative — it is already the root row.
    for wcase, case_key in CASE_ORDER[1:]:
        if case_key not in entry.singular:
            continue
        cursor.execute(
            """
            INSERT INTO nouns_morf
                (word, code, code_parent, plural, gender, wcase, soul,
                 is_custom, created_at, category)
            VALUES
                (%s, %s, %s, 0, %s, %s, %s, 1, %s, %s)
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
        code += 1

    # Insert plural declensions if present.
    if entry.plural:
        # Nominative plural is a child of the root.
        cursor.execute(
            """
            INSERT INTO nouns_morf
                (word, code, code_parent, plural, gender, wcase, soul,
                 is_custom, created_at, category)
            VALUES
                (%s, %s, %s, 1, NULL, 'им', %s, 1, %s, %s)
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
        plural_root_code = code
        code += 1

        # Remaining plural declensions are children of the nominative plural.
        for wcase, case_key in CASE_ORDER[1:]:
            if case_key not in entry.plural:
                continue
            cursor.execute(
                """
                INSERT INTO nouns_morf
                    (word, code, code_parent, plural, gender, wcase, soul,
                     is_custom, created_at, category)
                VALUES
                    (%s, %s, %s, 1, NULL, %s, %s, 1, %s, %s)
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
            code += 1

    verify_entry(cursor, root_code, entry)
    return root_code


def delete_entry(cursor: pymysql.cursors.Cursor, root_word: str) -> int:
    """Delete an existing entry and all its rows.

    Deletes in reverse dependency order: plural declensions,
    nominative plural, singular declensions, then the root row.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        root_word (str): The root row's word column value.

    Returns:
        int: Number of rows deleted (0 if entry not found).
    """
    root_code = entry_exists(cursor, root_word)
    if root_code is None:
        return 0

    rows_deleted = 0

    # Locate nominative plural (if it exists)
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
        plural_code = plural_result["code"]
        # Delete plural declensions, then the nominative plural row
        cursor.execute("DELETE FROM nouns_morf WHERE code_parent = %s", (plural_code,))
        rows_deleted += cursor.rowcount
        cursor.execute("DELETE FROM nouns_morf WHERE code = %s", (plural_code,))
        rows_deleted += cursor.rowcount

    # Delete singular declensions, then the root row
    cursor.execute("DELETE FROM nouns_morf WHERE code_parent = %s", (root_code,))
    rows_deleted += cursor.rowcount
    cursor.execute("DELETE FROM nouns_morf WHERE code = %s", (root_code,))
    rows_deleted += cursor.rowcount

    return rows_deleted


def fetch_entry_rows(cursor: pymysql.cursors.Cursor, root_code: int) -> list[dict]:
    """Fetch all rows for an entry (root, children, grandchildren).

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        root_code (int): The root row's code.

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
               SELECT code FROM nouns_morf
               WHERE code_parent = %s AND plural = 1
           )
        """,
        (root_code, root_code, root_code),
    )
    return cursor.fetchall()


def verify_entry(
    cursor: pymysql.cursors.Cursor, root_code: int, entry: NounEntry
) -> None:
    """Verify that an entry was inserted correctly.

    Queries back the inserted rows and checks each expected case form
    for correct word, wcase, plural flag, and parent code.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        root_code (int): The root row's code.
        entry (NounEntry): The entry to verify.

    Raises:
        ValueError: If verification fails.
    """
    if entry.indeclinable:
        _verify_indeclinable(cursor, root_code, entry)
        return

    rows = fetch_entry_rows(cursor, root_code)
    row_map = {(row["plural"], row["wcase"]): row for row in rows}

    _verify_root(row_map, entry, root_code)
    _verify_singular(row_map, entry, root_code)
    _verify_plural(row_map, entry, root_code)


def _verify_indeclinable(cursor, root_code, entry) -> None:
    """Verify an indeclinable entry (single row, wcase = NULL)."""
    cursor.execute(
        "SELECT word, code_parent, plural, wcase FROM nouns_morf WHERE code = %s",
        (root_code,),
    )
    rows = cursor.fetchall()

    if len(rows) != 1:
        raise ValueError(f"'{entry.word}': expected 1 row, found {len(rows)}")

    row = rows[0]
    _verify_field(entry.word, "word", entry.word, row["word"])
    _verify_field(entry.word, "parent code", 0, row["code_parent"])
    _verify_field(entry.word, "plural flag", 0, row["plural"])
    _verify_field(entry.word, "wcase", "NULL", row["wcase"])


def _verify_root(row_map, entry, root_code) -> None:
    """Verify the root row."""
    root = row_map.get((0, "им"))
    if root is None:
        raise ValueError(f"'{entry.word}': root row missing")

    _verify_field(entry.word, "root word", entry.singular["nominative"], root["word"])
    _verify_field(entry.word, "root code", root_code, root["code"])
    _verify_field(entry.word, "root parent code", 0, root["code_parent"])


def _verify_singular(row_map, entry, root_code) -> None:
    """Verify singular declensions (children of root)."""
    for wcase, case_key in CASE_ORDER[1:]:  # skip nominative (root)
        if case_key not in entry.singular:
            continue
        row = row_map.get((0, wcase))
        if row is None:
            raise ValueError(f"'{entry.word}': singular {wcase} row missing")
        _verify_field(
            entry.word, f"singular {wcase} word", entry.singular[case_key], row["word"]
        )
        _verify_field(
            entry.word, f"singular {wcase} parent code", root_code, row["code_parent"]
        )


def _verify_plural(row_map, entry, root_code) -> None:
    """Verify plural rows."""
    if not entry.plural:
        return

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

    plural_root_code = nom_plural["code"]

    for wcase, case_key in CASE_ORDER[1:]:
        if case_key not in entry.plural:
            continue
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


def _verify_field(entry_word, field_name, expected, actual) -> None:
    """Compare expected vs actual, raising with context on mismatch.

    Normalizes SQL NULL (None) to the string "NULL" on both sides
    before comparison, so callers can pass either representation.
    """
    if expected is None:
        expected = "NULL"
    if actual is None:
        actual = "NULL"

    if expected != actual:
        raise ValueError(
            f"'{entry_word}': {field_name} mismatch. "
            f"Expected {expected}, found {actual}"
        )


def query_entry(
    cursor: pymysql.cursors.Cursor, root_word: str, case_insensitive: bool = False
) -> list[dict]:
    """Query all rows for an entry by its root word.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.
        root_word (str): The root row's word column value.
        case_insensitive (bool): If True, match case-insensitively.

    Returns:
        list[dict]: All matching rows (empty if not found).
    """
    root_code = entry_exists(cursor, root_word, case_insensitive)
    if root_code is None:
        return []

    cursor.execute(
        """
        SELECT * FROM nouns_morf
        WHERE code = %s
           OR code_parent = %s
           OR code_parent IN (
               SELECT code FROM nouns_morf
               WHERE code_parent = %s AND plural = 1
           )
        ORDER BY plural, wcase
        """,
        (root_code, root_code, root_code),
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def sanity_check(config: dict) -> bool:
    """Run all DB sanity checks. Returns True if all pass.

    Checks indexes, data fixes, and custom columns. Logs PASS/FAIL
    for each individual check.

    Args:
        config (dict): MySQL connection settings.

    Returns:
        bool: True if all checks passed, False otherwise.
    """
    with db_transaction(config) as cursor:
        index_ok = _sanity_indexes(cursor)
        data_ok = _sanity_data_fixes(cursor)
        columns_ok = _sanity_custom_columns(cursor)

    return index_ok and data_ok and columns_ok


def _log_sanity_result(check_name: str, passed: bool) -> bool:
    """Log a PASS/FAIL line for a sanity check.

    Args:
        check_name (str): Human-readable check name.
        passed (bool): Whether the check passed.

    Returns:
        bool: The passed value unchanged, for aggregation.
    """
    if passed:
        logger.added(f"  PASS  {check_name}")
    else:
        logger.error(f"  FAIL  {check_name}")
    return passed


def _sanity_indexes(cursor: pymysql.cursors.Cursor) -> bool:
    """Check that expected indexes exist on nouns_morf.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.

    Returns:
        bool: True if all expected indexes exist.
    """
    expected = ["code_idx", "code_parent_idx", "word_idx"]

    cursor.execute("""
        SELECT DISTINCT INDEX_NAME
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'nouns_morf'
        """)
    existing = {row["INDEX_NAME"] for row in cursor.fetchall()}

    logger.notice("SANITY CHECK: indexes")
    results = []
    for index_name in expected:
        results.append(_log_sanity_result(index_name, index_name in existing))

    return all(results)


def _sanity_data_fixes(cursor: pymysql.cursors.Cursor) -> bool:
    """Check that suppletive plural fixes are in place.

    Verifies that дети has parent links to both дитя and ребенок.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.

    Returns:
        bool: True if both parent links exist.
    """
    logger.notice("SANITY CHECK: data fixes")
    results = []

    # Check дети -> дитя
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM nouns_morf c
        JOIN nouns_morf p ON p.code = c.code_parent
        WHERE c.word = 'дети' AND p.word = 'дитя'
        """)
    ditja_ok = cursor.fetchone()["cnt"] > 0
    results.append(_log_sanity_result("дети -> дитя", ditja_ok))

    # Check дети -> ребенок
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM nouns_morf c
        JOIN nouns_morf p ON p.code = c.code_parent
        WHERE c.word = 'дети' AND p.word = 'ребенок'
        """)
    rebenok_ok = cursor.fetchone()["cnt"] > 0
    results.append(_log_sanity_result("дети -> ребенок", rebenok_ok))

    return all(results)


def _sanity_custom_columns(cursor: pymysql.cursors.Cursor) -> bool:
    """Check that custom columns exist on nouns_morf.

    Verifies that is_custom, created_at, and category columns exist.

    Args:
        cursor (pymysql.cursors.Cursor): Active cursor.

    Returns:
        bool: True if all expected columns exist.
    """
    expected = ["is_custom", "created_at", "category"]

    cursor.execute("""
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'nouns_morf'
        """)
    existing = {row["COLUMN_NAME"] for row in cursor.fetchall()}

    logger.notice("SANITY CHECK: custom columns")
    results = []
    for column_name in expected:
        results.append(_log_sanity_result(column_name, column_name in existing))

    return all(results)


def add_file_entries(import_file: ImportFile, config: dict) -> FileResult:
    """Insert entries, skipping those that already exist.

    Args:
        import_file (ImportFile): The parsed and validated file.
        config (dict): MySQL connection settings.

    Returns:
        FileResult: The operation result.
    """
    result = FileResult(
        path=import_file.path, category=import_file.category, operation="add"
    )

    with db_transaction(config) as cursor:
        created_at = get_created_at(cursor)

        for entry in import_file.entries:
            root_word = entry.root_word()
            existing_code = entry_exists(cursor, root_word)

            if existing_code is not None:
                entry_result = EntryResult(
                    word=entry.word,
                    root_code=existing_code,
                    status="skipped",
                    rows_affected=0,
                )
                result.entries.append(entry_result)
                print_entry_result(entry_result)
                continue

            # get next available db code to insert an entry into
            next_code = get_next_code(cursor)
            # - new_root_code: code for root of new entry (should just be next_code)
            new_root_code = insert_entry(
                cursor, entry, import_file.category, next_code, created_at
            )
            entry_result = EntryResult(
                word=entry.word,
                root_code=new_root_code,
                status="added",
                rows_affected=entry.row_count(),
            )
            result.entries.append(entry_result)
            print_entry_result(entry_result)
            result.total_rows += entry.row_count()

    return result


def update_file_entries(import_file: ImportFile, config: dict) -> FileResult:
    """Replace existing entries and insert new ones.

    For each entry in the file: if it exists, delete it and re-insert
    with fresh data. If it doesn't exist, insert it.

    Args:
        import_file (ImportFile): The parsed and validated file.
        config (dict): MySQL connection settings.

    Returns:
        FileResult: The operation result.
    """
    result = FileResult(
        path=import_file.path, category=import_file.category, operation="update"
    )

    with db_transaction(config) as cursor:
        created_at = get_created_at(cursor)

        for entry in import_file.entries:
            root_word = entry.root_word()
            existing_code = entry_exists(cursor, root_word)

            if existing_code is not None:
                delete_entry(cursor, root_word)

            # get next available db code to insert an entry into
            next_code = get_next_code(cursor)
            # - new_root_code: code for root of new entry (should just be next_code)
            new_root_code = insert_entry(
                cursor, entry, import_file.category, next_code, created_at
            )

            status = "updated" if existing_code is not None else "added"
            entry_result = EntryResult(
                word=entry.word,
                root_code=new_root_code,
                status=status,
                rows_affected=entry.row_count(),
            )
            result.entries.append(entry_result)
            print_entry_result(entry_result)
            result.total_rows += entry.row_count()

    return result


def delete_file_entries(import_file: ImportFile, config: dict) -> FileResult:
    """Delete entries after explicit confirmation.

    Checks which entries exist, prompts the user to confirm, then
    deletes them. Entries that don't exist are reported as skipped.

    Args:
        import_file (ImportFile): The parsed and validated file.
        config (dict): MySQL connection settings.

    Returns:
        FileResult: The operation result (cancelled=True if user aborted).
    """
    result = FileResult(
        path=import_file.path, category=import_file.category, operation="delete"
    )

    with db_transaction(config) as cursor:
        # Gather existing entries
        to_delete = []
        for entry in import_file.entries:
            root_word = entry.root_word()
            existing_code = entry_exists(cursor, root_word)
            if existing_code is not None:
                to_delete.append((entry, existing_code, root_word))
            else:
                entry_result = EntryResult(
                    word=entry.word,
                    root_code=0,
                    status="not_found",
                    rows_affected=0,
                    error=True,
                    error_message=f"Cannot delete {entry.word}: Not found in database",
                )
                result.entries.append(entry_result)
                print_entry_result(entry_result)

        if not to_delete:
            return result

        # Prompt for confirmation
        if not _confirm_delete([(e.word, c) for e, c, _ in to_delete]):
            result.cancelled = True
            return result

        # Perform deletion
        for entry, existing_code, root_word in to_delete:
            rows_deleted = delete_entry(cursor, root_word)
            entry_result = EntryResult(
                word=entry.word,
                root_code=existing_code,
                status="deleted",
                rows_affected=rows_deleted,
            )
            result.entries.append(entry_result)
            print_entry_result(entry_result)
            result.total_rows += rows_deleted

    return result


def verify_file_entries(import_file: ImportFile, config: dict) -> FileResult:
    """Verify that entries in a JSON file exist and are correct in the DB.

    For each entry, checks existence and field-level correctness against
    the expected values from the JSON. Does not modify the database.

    Args:
        import_file (ImportFile): The parsed and validated file.
        config (dict): MySQL connection settings.

    Returns:
        FileResult: The verification result.
    """
    result = FileResult(
        path=import_file.path,
        category=import_file.category,
        operation="verify",
    )

    with db_transaction(config) as cursor:
        for entry in import_file.entries:
            root_word = entry.root_word()
            root_code = entry_exists(cursor, root_word)

            if root_code is None:
                entry_result = EntryResult(
                    word=entry.word,
                    root_code=0,
                    status="not_found",
                    rows_affected=0,
                    error=True,
                    error_message="Cannot verify {entry.word}: Not found in database.",
                )
                result.entries.append(entry_result)
                print_entry_result(entry_result)
                continue

            try:
                verify_entry(cursor, root_code, entry)
                entry_result = EntryResult(
                    word=entry.word,
                    root_code=root_code,
                    status="verified",
                    rows_affected=entry.row_count(),
                )
                result.entries.append(entry_result)
                print_entry_result(entry_result)
            except ValueError as e:
                entry_result = EntryResult(
                    word=entry.word,
                    root_code=root_code,
                    status="mismatch",
                    rows_affected=0,
                    detail=str(e),
                    error=True,
                    error_message=str(e),
                )
                result.entries.append(entry_result)
                print_entry_result(entry_result)

    return result


def _confirm_delete(entries: list[tuple[str, int]]) -> bool:
    """Prompt for explicit DELETE confirmation.

    Args:
        entries (list[tuple[str, int]]): List of (word, root_code)
            pairs to be deleted.

    Returns:
        bool: True if user typed DELETE (case-sensitive).
    """
    noun = "entry" if len(entries) == 1 else "entries"
    logger.info(f"\nYou are about to delete {len(entries)} {noun}:\n")

    for word, code in entries:
        logger.skipped(f"  = {word} (code={code})")

    logger.error("\nThis action CANNOT be undone. Type 'DELETE' to confirm: ")
    response = input()
    return response == "DELETE"


def show_word(word: str, config: dict) -> None:
    """Query and display a single entry by word.

    Args:
        word (str): The word to look up.
        config (dict): MySQL connection settings.
    """
    with db_transaction(config) as cursor:
        # for basic --show operation, allow case insensitive matching
        # (--show иван should match Иван)
        rows = query_entry(cursor, word, case_insensitive=True)

    if not rows:
        logger.info(f"No rows found for '{word}'")
    else:
        logger.table(rows, word)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_file_header(path: Path) -> None:
    """Print a header showing which file is being processed.

    Args:
        path (Path): The file being processed.
    """
    logger.notice(f"\n─── {path.as_posix()} ───\n")


def print_entry_result(entry_result: EntryResult) -> None:
    """Print a single entry result with the appropriate logger action."""
    if entry_result.status == "added":
        logger.added(
            f"  + {entry_result.word} (code={entry_result.root_code}): {entry_result.rows_affected} rows"
        )
    elif entry_result.status == "updated":
        logger.updated(
            f"  ~ {entry_result.word} (code={entry_result.root_code}): {entry_result.rows_affected} rows"
        )
    elif entry_result.status == "deleted":
        logger.deleted(
            f"  ~ {entry_result.word} (code={entry_result.root_code}): deleted {entry_result.rows_affected} rows"
        )
    elif entry_result.status == "skipped":
        logger.skipped(
            f"  = {entry_result.word} (code={entry_result.root_code}, already exists, skipped)"
        )
    elif entry_result.status == "not_found":
        logger.skipped(f"  = {entry_result.word} (not found, skipped)")
    elif entry_result.status == "verified":
        logger.added(
            f"  ✓ {entry_result.word} (code={entry_result.root_code}): verified {entry_result.rows_affected} rows"
        )
    elif entry_result.status == "mismatch":
        logger.error(
            f"  ✗ {entry_result.word} (code={entry_result.root_code}): {entry_result.detail}"
        )


def print_file_summary(result: FileResult, file_count: int | None) -> None:
    """Print a summary of an operation's results.

    Args:
        result (FileResult): The operation result.
        operation (str): The operation name (e.g. 'add', 'update',
            'delete', 'verify', 'validate-json').
        count (int): Optional file index to add to header
    """

    separator = "═" * 60

    file_int_str = f"(#{str(file_count)})" if file_count else ""
    logger.notice(f"\n{separator}")
    logger.notice(f" FILE RESULT SUMMARY {file_int_str}")
    logger.notice(separator)
    logger.notice(f"   File:      {result.path.as_posix()}")
    logger.notice(f"   Operation: {result.operation}")
    logger.notice(f"   Category:  {result.category}")
    logger.notice(separator)

    if result.cancelled:
        logger.notice("\nDeletion cancelled. No entries were deleted.")
        return

    if not result.entries:
        logger.notice("\nNothing to do.")
        return

    # Print per-entry results in order of appearance
    for entry in result.entries:
        print_entry_result(entry)

    # Count statuses
    added = [e for e in result.entries if e.status == "added"]
    updated = [e for e in result.entries if e.status == "updated"]
    deleted = [e for e in result.entries if e.status == "deleted"]
    skipped = [e for e in result.entries if e.status == "skipped"]
    not_found = [e for e in result.entries if e.status == "not_found"]

    # Print summary line
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if updated:
        parts.append(f"{len(updated)} updated")
    if deleted:
        parts.append(f"{len(deleted)} deleted")
    if skipped:
        parts.append(f"{len(skipped)} skipped")
    if not_found:
        parts.append(f"{len(not_found)} not found")

    logger.info(f"\nSUMMARY: {', '.join(parts)}, {result.total_rows} rows affected")

    # Print any additional info
    if result.notice:
        logger.notice(result.notice)


def print_job_summary(results: list[FileResult]) -> None:
    """Print a consolidated summary of all file results.

    Args:
        results (list[FileResult]): All file results from the job.
    """
    separator = "═" * 60

    logger.notice(f"\n{separator}")
    logger.notice("JOB COMPLETE")
    logger.notice(separator)

    total_rows = sum(r.total_rows for r in results)
    total_entries = sum(len(r.entries) for r in results)

    logger.info(f"  Files processed: {len(results)}")
    logger.info(f"  Entries:         {total_entries}")
    logger.info(f"  Rows affected:   {total_rows}")

    logger.notice(separator)

    for i, result in enumerate(results):
        print_file_summary(result, i + 1)


def build_validation_result(import_file: ImportFile) -> FileResult:
    """Build a FileResult for validation without touching the DB.

    Simulates code assignment and row counts so the output matches
    what an actual import would look like.

    Args:
        import_file (ImportFile): The parsed and validated file.

    Returns:
        FileResult: The simulated result.
    """
    result = FileResult(
        path=import_file.path, category=import_file.category, operation="add"
    )

    simulated_code = 1
    for entry in import_file.entries:
        result.entries.append(
            EntryResult(
                word=entry.word,
                root_code=simulated_code,
                status="added",
                rows_affected=entry.row_count(),
            )
        )
        result.total_rows += entry.row_count()
        simulated_code += entry.row_count()

    return result


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


def print_job_errors(errors: list[ErrorResult]) -> None:
    """Print all job errors grouped by file and general errors.

    Args:
        errors (list[ErrorResult]): The errors to print.
    """
    separator = "═" * 60

    logger.notice(f"\n{separator}")
    logger.notice("JOB ERRORS")
    logger.notice(separator)

    # Group file-specific errors by file path
    file_errors: dict[Path, list[ErrorResult]] = {}
    for error in errors:
        if not error.general and error.file is not None:
            file_errors.setdefault(error.file.path, []).append(error)

    for file_path, file_error_list in file_errors.items():
        logger.error(f"\n  File: {file_path.as_posix()}")
        for error in file_error_list:
            message = error.message or "(no error details)"
            if error.entry is not None and error.entry.word:
                logger.error(f"    Entry: {error.entry.word} — {message}")
            else:
                logger.error(f"    {message}")

    # Print general errors
    general_errors = [e for e in errors if e.general]
    if general_errors:
        logger.error(f"\n  General errors:")
        for error in general_errors:
            message = error.message or "(no error details)"
            logger.error(f"    {message}")

    logger.notice(separator)


def check_job_errors(results: list[FileResult]) -> list[ErrorResult]:
    """Collect all entry-level errors from a job's file results.

    Iterates through each FileResult's entries and collects any
    EntryResult where the `error` flag is set, wrapping each in an
    ErrorResult with the originating file and entry context.

    Args:
        results (list[FileResult]): All file results from the job.

    Returns:
        list[ErrorResult]: List of errors collected from entries.
    """
    errors = []
    for fileResult in results:
        for entry in fileResult.entries:
            if entry.error:
                errors.append(
                    ErrorResult(
                        file=fileResult, entry=entry, message=entry.error_message
                    )
                )
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Supports two command sets:
      1. File operations: operation + path, with optional
         --no-recursive and --no-dump flags.
      2. Utility operations: (no JSON path(s)):
         --word WORD, --sanity

    Common flags (--config, --color, --stderr) are shared across
    both command sets via a parent parser.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    # Parent parser with flags shared by all operations
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to MySQL config file (default: .mysql_import.conf in cwd, home, or /etc)",
    )
    common.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color output: auto (default), always, or never",
    )
    common.add_argument(
        "--stderr",
        action="store_true",
        help="Route all output to stderr",
    )

    parser = argparse.ArgumentParser(
        description="Manage custom Russian noun declensions in the runouns database.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # File operations subcommand
    file_parser = subparsers.add_parser(
        "file",
        parents=[common],
        help="File-based operations on JSON entries",
    )
    file_parser.add_argument(
        "path",
        type=Path,
        help="JSON file or directory",
    )
    file_parser.add_argument(
        "operation",
        choices=[
            "add",
            "update",
            "delete",
            "validate-json",
            "verify",
            "verify-all",
        ],
        help="Operation to perform",
    )
    file_parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not process subdirectories when path is a directory",
    )
    file_parser.add_argument(
        "--no-dump",
        action="store_true",
        help="Do not print full rows after add/update",
    )

    # Standalone operations subcommand
    utility_parser = subparsers.add_parser(
        "util",
        parents=[common],
        help="Standalone operations",
    )
    utility_parser.add_argument(
        "--word",
        type=str,
        metavar="WORD",
        help="Display an entry by word",
    )
    utility_parser.add_argument(
        "--sanity",
        action="store_true",
        help="Run DB sanity checks",
    )

    return parser.parse_args()


def main() -> None:
    """Parse arguments and dispatch to the appropriate operation.

    Handles shared setup (color, logger, config loading) before
    delegating to either the utility or file-based operation handler.
    """
    args = parse_args()

    # Determine color settings
    if args.color == "always":
        color_enabled = True
    elif args.color == "never":
        color_enabled = False
    else:
        color_enabled = sys.stdout.isatty()

    # Configure the logger
    logger.color_enabled = color_enabled
    logger.use_stderr = args.stderr

    # Load config
    try:
        config = load_config(args.config)
    except (FileNotFoundError, configparser.Error) as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)

    # Utility operations (no JSON files)
    if args.command == "util":
        handle_util_operations(args, config)
    # JSON file operations (delete, update, add words, etc.)
    elif args.command == "file":
        handle_file_operations(args, config)


def handle_util_operations(args: argparse.Namespace, config: dict) -> None:
    """Handle utility operations that do not require JSON files.

    Supports --word (display an entry) and --sanity (run DB sanity
    checks). Both operations exit directly after completion.

    Args:
        args (argparse.Namespace): Parsed command line arguments.
            Must have been invoked via the 'util' subparser. e.g.,
                python manager.py util --word кролик
                python manager.py util --sanity
            See parse_args() for the full set of attributes.
        config (dict): MySQL connection settings.
    """

    # Query a single entry from the database
    if args.word:
        show_word(args.word, config)

    # Run sanity check
    if args.sanity:
        if not sanity_check(config):
            sys.exit(1)


def handle_file_operations(args: argparse.Namespace, config: dict) -> None:
    """Handle file-based operations on JSON entries.

    Resolves the path (file or directory) into a list of JSON files,
    dispatches each to the appropriate operation, collects results
    and errors, then prints the job summary and exits with an
    appropriate status code.

    Args:
        args (argparse.Namespace): Parsed command line arguments.
            Must have been invoked via the 'file' subparser. e.g.,
                python manager.py file custom-json/ add
            See parse_args() for the full set of attributes.
        config (dict): MySQL connection settings.
    """

    # Resolve paths for file-based operations
    json_files = _resolve_paths(args.path, args.no_recursive)
    if not json_files:
        logger.error(f"No .json files found in {args.path}")
        sys.exit(1)

    # special sanity check only verifies phase 1 transformations
    # This is handled separately as it's a per DB
    # call (not per JSON file)
    if args.operation == "verify-all":
        if not sanity_check(config):
            sys.exit(1)

    exit_code = 0
    file_results = []
    errors = []
    # Perform appropriate action
    for json_file in json_files:
        try:
            import_file = ImportFile.from_file(json_file)
            print_file_header(json_file)

            if args.operation == "validate-json":
                result = build_validation_result(import_file)
                # add addendum which will display in print_file_summary
                result.notice = (
                    "(code values are simulated; no database connection was made)"
                )
            if args.operation == "add":
                result = add_file_entries(import_file, config)
            elif args.operation == "update":
                result = update_file_entries(import_file, config)
            elif args.operation == "delete":
                result = delete_file_entries(import_file, config)
            elif args.operation in ("verify", "verify-all"):
                result = verify_file_entries(import_file, config)

            file_results.append(result)

            # Dump rows after add/update unless --no-dump
            if args.operation in ("add", "update") and not args.no_dump:
                _dump_inserted_entries(result, config)
        except (ValueError, json.JSONDecodeError) as e:
            errors.append(
                ErrorResult(
                    message=f"Validation error in {json_file}: {e}", general=True
                )
            )
        except pymysql.MySQLError as e:
            errors.append(
                ErrorResult(message=f"Database error in {json_file}: {e}", general=True)
            )
        except Exception as e:
            errors.append(
                ErrorResult(
                    message=f"Unexpected error processing {json_file}: {e}",
                    general=True,
                )
            )

    print_job_summary(file_results)
    errors.extend(check_job_errors(file_results))
    if errors:
        print_job_errors(errors)
        exit_code = 1

    sys.exit(exit_code)


def _resolve_paths(path: Path, no_recursive: bool = False) -> list[Path]:
    """Resolve a file or directory path to a list of JSON files.

    Args:
        path (Path): A JSON file or directory.
        no_recursive (bool): If True and path is a directory, only
            match JSON files in the top level.

    Returns:
        list[Path]: Sorted list of JSON file paths.
    """
    if path.is_file():
        return [path]
    if path.is_dir():
        if no_recursive:
            return sorted(path.glob("*.json"))
        return sorted(path.rglob("*.json"))
    return []


def _dump_inserted_entries(result: FileResult, config: dict) -> None:
    """Print full rows for entries that were added or updated.

    Args:
        result (FileResult): The operation result.
        config (dict): MySQL connection settings.
    """
    with db_transaction(config) as cursor:
        for entry in result.entries:
            if entry.status in ("added", "updated"):
                rows = query_entry(cursor, entry.word)
                logger.table(rows, entry.word)


if __name__ == "__main__":
    main()
