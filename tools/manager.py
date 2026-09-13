#!/usr/bin/env python3
"""rumorph: manage custom Russian noun entries in the runouns database.

Reads JSON files describing Russian nouns and adds, updates, deletes, or
verifies the matching rows in the nouns_morf MySQL table. Also provides a
word lookup and a database sanity check.

USAGE

    python manager.py entries <path> <operation> [flags]
    python manager.py util --word <word> [flags]
    python manager.py util --sanity [flags]

    Operations for entries: add, update, delete, verify, verify-all,
    validate-json.

    Exit codes: 0 = success, 1 = error, 2 = warnings only.

DATABASE LAYOUT

Table nouns_morf. One row per case form. Rows form a tree per noun:

    root (code_parent = 0)             the dictionary entry
    ├── singular cases                 code_parent = root.code, plural = 0
    └── nominative plural              code_parent = root.code, plural = 1
        └── other plural cases         code_parent = nominative plural's code

The dictionary form is the nominative singular for normal nouns, or the
nominative plural for plural-only nouns. A noun may have more than one
nominative plural (человек has люди and человеки).

New rows get code = max(code) + 1. The database does not enforce
uniqueness on code, so the tool assumes it is the only writer.

JSON FORMAT

One file per category:

    {
      "category": "имя",
      "entries": [
        {
          "word": "Иван",
          "gender": "муж",
          "animate": true,
          "singular": {"nominative": "Иван", "genitive": "Ивана", ...},
          "plural": {"nominative": "Иваны", ...}
        }
      ]
    }

- word is the dictionary form. It must equal the nominative of the first
  block present.
- gender is one of муж, жен, ср, общ.
- animate is true or false.
- singular and plural are blocks of case forms. All six standard cases
  are required in each block. The four rare cases (vocative, partitive,
  locative, counting) are optional.
- A plural-only noun omits singular. A singular-only noun omits plural.
- An indeclinable noun has "indeclinable": true and no blocks.

OPERATIONS

add      Insert unless an entry with the same content exists.
update   Replace the tree for a word. N=0 inserts, N=1 replaces (or
         no-ops if identical), N>1 refuses unless --force-update.
delete   Delete trees matching the entry's content. N>1 refuses unless
         --force-delete.
verify   Read-only. Check the database against each entry.
verify-all
         Run the transformation checks once. Does not process files.
validate-json
         Check the JSON against the schema. Does not touch the database.

CASE SENSITIVITY

Word comparisons are case-sensitive by default and use the word prefix
index. With --case-insensitive, they ignore case (иван matches Иван) at
the cost of a full table scan, because LOWER(word) cannot use the index.
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import traceback

import pymysql
from pymysql.connections import Connection

VERSION = "0.1.0"
"""Version string, printed by --version."""

WORD_COLLATION = "utf8mb4_bin"
"""Collation applied to word comparisons to distinguish е from ё.

The word column is utf8mb3, which does not accept utf8mb4_bin directly,
so the collation goes on the parameter side of the comparison:
"WHERE word = %s COLLATE utf8mb4_bin".
"""

ROOT_PARENT = 0
"""code_parent value for a root row."""

CONFIG_FILENAME = ".mysql_import.conf"
"""Config file searched for in current dir, home, then /etc."""

COLUMNS = "IID, word, code, code_parent, plural, gender, wcase, soul, is_custom, created_at, category"
"""The columns every SELECT reads. Listed explicitly so a new column
added to the schema does not silently appear in results."""


# =============================================================================
# Enums
# =============================================================================


class Gender(str, Enum):
    """Grammatical gender. Values match the gender column."""

    MASCULINE = "муж"
    FEMININE = "жен"
    NEUTER = "ср"
    COMMON = "общ"


class Case(str, Enum):
    """Grammatical case. Values match the wcase column.

    The first six are required for declinable nouns. The last four are
    rare and optional.
    """

    NOMINATIVE = "им"
    GENITIVE = "род"
    DATIVE = "дат"
    ACCUSATIVE = "вин"
    INSTRUMENTAL = "тв"
    PREPOSITIONAL = "пр"
    VOCATIVE = "зват"
    PARTITIVE = "парт"
    LOCATIVE = "мест"
    COUNTING = "счет"


STANDARD_CASES = (
    Case.NOMINATIVE,
    Case.GENITIVE,
    Case.DATIVE,
    Case.ACCUSATIVE,
    Case.INSTRUMENTAL,
    Case.PREPOSITIONAL,
)
"""Cases required in every declension block."""

CASE_TO_JSON = {
    Case.NOMINATIVE: "nominative",
    Case.GENITIVE: "genitive",
    Case.DATIVE: "dative",
    Case.ACCUSATIVE: "accusative",
    Case.INSTRUMENTAL: "instrumental",
    Case.PREPOSITIONAL: "prepositional",
    Case.VOCATIVE: "vocative",
    Case.PARTITIVE: "partitive",
    Case.LOCATIVE: "locative",
    Case.COUNTING: "counting",
}
"""Case to JSON key. The JSON keys happen to equal the Declension field
names, but they are separate concepts and could diverge."""

JSON_TO_CASE = {v: k for k, v in CASE_TO_JSON.items()}


class ResultOutcome(str, Enum):
    """Identifiers for the result state of a single operation or check.

    These are not semantic values. They are identifiers whose only jobs
    are to be compared against each other and looked up in the symbol
    map used by the end-of-run summary printing. They are consumed by
    Result objects (as the outcome field), by RESULT_SYMBOLS (as lookup
    keys), and by the summary printers (which count them and print them
    directly). Nothing outside those uses depends on their values.

    str subclass so that comparisons, dict lookups, and f-string
    formatting against the underlying strings keep working unchanged.
    """

    ADDED = "added"
    UPDATED = "updated"
    DELETED = "deleted"
    SKIPPED = "skipped"
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    NOT_FOUND = "not_found"
    NO_CHANGE = "no_change"
    ERROR = "error"
    TRANSFORMATION_OK = "transformation_ok"
    TRANSFORMATION_FAIL = "transformation_fail"


# =============================================================================
# Errors
# =============================================================================


class RumorphError(Exception):
    """Base for expected failures: bad config, bad JSON, bad database.
    Anything not deriving from this is a bug and gets a traceback."""


class ConfigError(RumorphError):
    """Config file missing, unreadable, or incomplete."""


class DbError(RumorphError):
    """Connection to MySQL failed."""


class ParseError(RumorphError):
    """A JSON entry does not satisfy the schema."""


# =============================================================================
# Logging
# =============================================================================


class Colors(str, Enum):
    # ANSI escape codes.
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    DIM = "\033[2m"
    BRIGHT_RED = "\033[91m"
    URGENT_RED = "\033[1;4;91m"
    BRIGHT_YELLOW = "\033[93m"
    BOLD_BLUE = "\033[1;34m"


class Logger:
    """All user-facing output goes through here.

    Holds the output stream, the minimum level, and the color setting.
    Every write is flushed so long-running commands show live output.

    Levels: a message prints if its level is at or above the configured
    minimum. The one exception is always(), which ignores the level and is
    used for output that is the direct answer to a command (the version
    string, a word-lookup table) rather than a log line.
    """

    LEVELS = {
        "debug": 10,
        "info": 20,
        "operation": 25,  # messages for operation output
        "warn": 30,
        "error": 40,
        "critical": 50,
        "always": 100,  # always print
    }

    def __init__(
        self, level=None, verbose=False, quiet=False, use_color=False, stream=sys.stdout
    ):
        """Initialize the logger.

        Args:
            stream: Where output goes (sys.stdout, or sys.stderr)
            level (str): The minimum level to print.
            use_color (bool): If True, emit ANSI color codes.
        """
        self.set_level(level, quiet, verbose)
        self._stream = stream
        self._color = use_color
        self._verbose = verbose
        self._quiet = quiet

    @classmethod
    def get_levels(cls):
        return sorted(k for k in cls.LEVELS if k != "always")

    def set_level(self, level, quiet, verbose):
        """Sets log threshold for Logger instance to a value in LEVELS"""
        if verbose and quiet:
            raise ValueError("Logger: can't set both quiet and verbose")
        if verbose:
            if level is not None and level != "debug":
                raise ValueError(
                    f"Logger: If verbose set, level must be debug. (got: {level}"
                )
            level = "debug"
        if quiet:
            if level is not None and level != "error":
                raise ValueError(
                    f"Logger: If quiet set, level must be error. (got: {level}"
                )
            level = "error"
        if level not in self.LEVELS:
            raise ValueError(
                f"{level} is not a valid log level. Valid log levels: {', '.join(self.LEVELS.keys())}"
            )
        self._level = self.LEVELS.get(level)

    def _should_print(self, msg_level):
        if msg_level == "always":
            return True
        return self.LEVELS[msg_level] >= self._level

    def _write(
        self, text: str, level: str, code: str = None, end: str = "\n", stream=None
    ) -> None:
        """Write one line if its level passes, with optional color.

        Args:
            level: The message's level, or None to always print.
            text: The line to write.
            code: The ANSI color for the whole line, or None.
        """
        # Determine if should print
        if not self._should_print(level):
            return

        # Determine stream routing
        stream = sys.stderr if self._stream == sys.stderr else stream

        # apply color to the entire line if enabled.
        if self._color and code is not None:
            text = f"{code}{text}{Colors.RESET}"

        # write and flush. Flushing is what makes output live.
        print(text, file=stream, end=end, flush=True)

    # --- diagnostic levels ---

    def debug(self, text: str, code: str = None, end="\n") -> None:
        self._write(text, level="debug", code=code, end=end, stream=sys.stderr)

    def info(self, text: str, code: str = None, end="\n") -> None:
        self._write(text, level="info", code=code, end=end, stream=sys.stdout)

    def warning(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.BRIGHT_YELLOW
        self._write(text, level="warn", code=color, end=end, stream=sys.stderr)

    def error(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.BRIGHT_RED
        self._write(text, level="error", code=color, end=end, stream=sys.stderr)

    def critical(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.URGENT_RED
        self._write(text, level="critical", code=color, end=end, stream=sys.stderr)

    # --- per-entry result lines ---

    def entry_added(self, text: str) -> None:
        """Print a per-entry 'added' line. Level: OPERATION, green, '+ '."""
        self._write(f"+ {text}", level="operation", code=Colors.GREEN)

    def entry_updated(self, text: str) -> None:
        """Print a per-entry 'updated' line. Level: OPERATION, yellow, '~ '."""
        self._write(f"~ {text}", level="operation", code=Colors.YELLOW)

    def entry_deleted(self, text: str) -> None:
        """Print a per-entry 'deleted' line. Level: OPERATION, red, '- '."""
        self._write(f"- {text}", level="operation", code=Colors.RED)

    def entry_skipped(self, text: str) -> None:
        """Print a per-entry 'skipped' line. Level: OPERATION, dim, 'x '."""
        self._write(f"x {text}", level="operation", code=Colors.DIM)

    def entry_matched(self, text: str) -> None:
        """Print a per-entry 'matched' line. Level: OPERATION, cyan, '✓ '."""
        self._write(f"✓ {text}", level="operation", code=Colors.CYAN)

    def entry_mismatched(self, text: str) -> None:
        """Print a per-entry 'mismatched' line. Level: OPERATION, red, '✗ '."""
        self._write(f"✗ {text}", level="warn", code=Colors.BRIGHT_RED)

    def entry_failed(self, text: str) -> None:
        """Print a per-entry failure line.

        Level: ERROR, so failures still show when --log-level is raised.
        Bright red, '! '.
        """
        self._write(f"! {text}", level="error", code=Colors.BRIGHT_RED)

    # --- structure ---

    def header(self, text: str) -> None:
        """Print a section header. Level: OPERATION, bold.

        Rendered as a line of the form "─── text ───".
        """
        self._write(f"─── {text} ───", level="operation", code=Colors.BOLD)

    def summary(self, text: str) -> None:
        """Print a summary line or block. Level: OPERATION.

        The caller is responsible for the content; embedded newlines are
        written as-is.
        """
        self._write(text, level="operation")

    # --- unconditional ---

    def always(self, text: str, code: str = None) -> None:
        """Print regardless of level.

        Used for output that is the direct answer to a command: the
        version string, the table from util --word. Not subject to
        --log-level filtering.
        """
        self._write(text, level="always", code=code)


# =============================================================================
# Data types
# =============================================================================


@dataclass(frozen=True)
class Declension:
    """The case forms of a noun within one number.

    Ten fields, one per case, each the surface form or None. The six
    standard cases are always populated for declinable nouns (enforced by
    parse_entry); the four rare cases are optional.
    """

    nominative: str | None = None
    genitive: str | None = None
    dative: str | None = None
    accusative: str | None = None
    instrumental: str | None = None
    prepositional: str | None = None
    vocative: str | None = None
    partitive: str | None = None
    locative: str | None = None
    counting: str | None = None

    def get(self, case: Case) -> str | None:
        """Return the form for a case, or None."""
        return getattr(self, case.name.lower())


@dataclass(frozen=True)
class NounEntry:
    """A noun in canonical form, built from JSON or from database rows.

    Because both sources produce this same type, an entry from a JSON file
    can be compared with == against one reconstructed from the database.
    That is what add, update, delete, and verify rely on.

    Shape:
        indeclinable:  singular and plural are both None.
        singular-only: plural is None.
        plural-only:   singular is None.
        normal:        both set.
    """

    word: str
    gender: Gender | None
    animate: bool | None
    category: str | None
    indeclinable: bool
    singular: Declension | None
    plural: tuple[Declension, ...] | None


@dataclass(frozen=True)
class NounRow:
    """One row from nouns_morf, with columns translated to domain names.

    wcase becomes case; soul becomes animate. Nullable tinyint columns
    become bool or None. The translation happens in row_from_dict.
    """

    IID: int
    word: str
    code: int
    code_parent: int
    plural: bool | None
    gender: Gender | None
    case: Case | None
    animate: bool | None
    is_custom: bool
    created_at: object
    category: str | None


@dataclass(kw_only=True)
class Result:
    """One reportable outcome from any operation.

    outcome is a short name: added, updated, deleted, skipped, matched,
    mismatched, not_found, no_change, or error. message describes the
    problem, if any. error and warning are flags.

    The outcomes themselves carry no syntactic meaning. They are
    identifiers used to key the symbol map and count results in the
    end-of-run summary. New ones can be added to ResultOutcome as
    needed; see its docstring for details.

    kind identifies what produced the result: "entry" for one noun
    entry, "check" for one sanity check, "file" for a file-level
    result, and so on. It exists so that code can filter results by
    origin without isinstance checks against the subclasses. The
    subclasses set it; the base default is "result".
    """

    outcome: ResultOutcome
    message: str | None = None
    error: bool = False
    warning: bool = False
    kind: str = "result"

    @property
    def label(self) -> str:
        """Short human label for this result."""
        return self.outcome


@dataclass(kw_only=True)
class EntryResult(Result):
    """The outcome of one operation on one entry."""

    word: str
    root_code: int = 0
    rows_affected: int = 0
    kind: str = "entry"

    @property
    def label(self) -> str:
        return self.word


@dataclass(kw_only=True)
class TransformResult(Result):
    """The outcome of one sanity check."""

    # human readable summary of the check
    check: str
    kind: str = "check"

    @property
    def label(self) -> str:
        return self.check


@dataclass
class Counts:
    """Tally of outcomes, for summaries."""

    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    matched: int = 0
    mismatched: int = 0
    not_found: int = 0
    failed: int = 0

    def add_result(self, r: EntryResult) -> None:
        """Increment the counter for a result's outcome."""
        if r.error:
            self.failed += 1
            return
        setattr(self, r.outcome.value, getattr(self, r.outcome.value) + 1)

    def add_counts(self, other: "Counts") -> None:
        """Add another Counts into this one."""
        for name in (
            "added",
            "updated",
            "deleted",
            "skipped",
            "matched",
            "mismatched",
            "not_found",
            "failed",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def summary(self) -> str:
        """Return a one-line summary of the nonzero counts."""
        parts = []
        for name in (
            "added",
            "updated",
            "deleted",
            "skipped",
            "matched",
            "mismatched",
            "not_found",
            "failed",
        ):
            n = getattr(self, name)
            if n:
                parts.append(f"{n} {name.replace('_', ' ')}")
        return ", ".join(parts) if parts else "no entries processed"


# =============================================================================
# Config and connection
# =============================================================================


def load_config(explicit_path: Path | None) -> dict:
    """Read the .mysql_import.conf file and return its [mysql] section.

    When explicit_path is None, searches ./.mysql_import.conf, ~/.mysql_import.conf,
    then /etc/.mysql_import.conf, using the first found. Every field is
    required; there are no defaults.

    Args:
        explicit_path: A path from --config, or None.

    Returns:
        A dict with host, port (int), user, password, database, table,
        charset.

    Raises:
        ConfigError: If the file is missing, unparsable, or incomplete.
    """
    # Step 1: locate the file.
    if explicit_path is not None:
        path = explicit_path
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        candidates = [
            Path(".") / CONFIG_FILENAME,
            Path.home() / CONFIG_FILENAME,
            Path("/etc") / CONFIG_FILENAME,
        ]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise ConfigError(f"no {CONFIG_FILENAME} in current dir, home, or /etc")

    # Step 2: parse INI.
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc

    if not parser.has_section("mysql"):
        raise ConfigError(f"{path} is missing the [mysql] section")

    # Step 3: require every field.
    section = dict(parser["mysql"])
    for key in ("host", "port", "user", "password", "database", "table", "charset"):
        if key not in section:
            raise ConfigError(f"{path} is missing required field: {key}")

    # Step 4: convert port to int; INI values are strings.
    try:
        section["port"] = int(section["port"])
    except ValueError as exc:
        raise ConfigError(
            f"{path}: port must be an integer, got {section['port']!r}"
        ) from exc

    return section


def connect(config: dict) -> Connection:
    """Open a MySQL connection from the config dict.

    Args:
        config: The [mysql] section.

    Returns:
        A pymysql connection with autocommit off.

    Raises:
        DbError: If the connection fails.
    """
    try:
        return pymysql.connect(
            host=config["host"],
            port=config["port"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            charset=config["charset"],
            autocommit=False,
        )
    except pymysql.Error as exc:
        raise DbError(
            f"could not connect to {config['host']}:{config['port']}"
            f"/{config['database']}: {exc}"
        ) from exc


# =============================================================================
# Row and entry conversion
# =============================================================================


def row_from_dict(d: dict) -> NounRow:
    """Convert a raw database row into a NounRow.

    Translates wcase to case and soul to animate, and converts tinyint
    columns to bool.

    Args:
        d: A DictCursor row.

    Returns:
        A NounRow.
    """
    return NounRow(
        IID=d["IID"],
        word=d["word"],
        code=d["code"],
        code_parent=d["code_parent"],
        plural=None if d["plural"] is None else bool(d["plural"]),
        gender=Gender(d["gender"]) if d["gender"] is not None else None,
        case=Case(d["wcase"]) if d["wcase"] is not None else None,
        animate=None if d["soul"] is None else bool(d["soul"]),
        is_custom=bool(d["is_custom"]),
        created_at=d["created_at"],
        category=d["category"],
    )


# =============================================================================
# JSON parsing
# =============================================================================


def parse_entry(raw: dict, category: str) -> NounEntry:
    """Parse and validate one JSON entry.

    The shape is inferred from the blocks present: indeclinable means no
    blocks, only singular means singular-only, only plural means
    plural-only, both means a normal noun.

    Args:
        raw: The entry dict.
        category: The file-level category.

    Returns:
        The parsed NounEntry.

    Raises:
        ParseError: On any missing or invalid field.
    """
    # Step 1: word, the dictionary form.
    word = raw.get("word")
    if not isinstance(word, str) or not word:
        raise ParseError("entry is missing a non-empty 'word'")

    # Step 2: gender.
    gender_str = raw.get("gender")
    if gender_str not in (g.value for g in Gender):
        raise ParseError(
            f"gender must be one of {[g.value for g in Gender]}, " f"got {gender_str!r}"
        )
    gender = Gender(gender_str)

    # Step 3: animate, a boolean. The JSON uses true/false, not 0/1.
    if "animate" not in raw:
        raise ParseError("entry is missing 'animate'")
    animate = raw["animate"]
    if not isinstance(animate, bool):
        raise ParseError(f"animate must be true or false, got {animate!r}")

    # Step 4: indeclinable, optional boolean.
    indeclinable = raw.get("indeclinable", False)
    if not isinstance(indeclinable, bool):
        raise ParseError("indeclinable must be true or false")

    # Step 5: indeclinable nouns have no declension blocks.
    if indeclinable:
        if "singular" in raw or "plural" in raw:
            raise ParseError("indeclinable entries must not have singular or plural")
        return NounEntry(
            word=word,
            gender=gender,
            animate=animate,
            category=category,
            indeclinable=True,
            singular=None,
            plural=None,
        )

    # Step 6: at least one of singular or plural must be present.
    has_singular = "singular" in raw
    has_plural = "plural" in raw
    if not has_singular and not has_plural:
        raise ParseError("entry must have singular or plural, or be indeclinable")

    # Step 7: parse the blocks.
    singular = _parse_declension(raw["singular"]) if has_singular else None
    plural = None
    if has_plural:
        block = raw["plural"]
        forms = [block] if isinstance(block, dict) else block
        if not isinstance(forms, list) or not forms:
            raise ParseError("plural must be an object or non-empty list")
        plural = tuple(_parse_declension(f) for f in forms)

    # Step 8: word must equal the nominative of the first block.
    expected = singular.nominative if singular is not None else plural[0].nominative
    if word != expected:
        raise ParseError(
            f"word {word!r} does not match the nominative form {expected!r}"
        )

    return NounEntry(
        word=word,
        gender=gender,
        animate=animate,
        category=category,
        indeclinable=False,
        singular=singular,
        plural=plural,
    )


def _parse_declension(block: dict) -> Declension:
    """Parse one declension block.

    Every key must be a known case name; every value a non-empty string;
    the six standard cases must all be present.

    Args:
        block: The dict of case keys to surface forms.

    Returns:
        The Declension.

    Raises:
        ParseError: On unknown keys, bad values, or missing standard cases.
    """
    if not isinstance(block, dict):
        raise ParseError("declension block must be an object")

    # Step 1: collect values, rejecting unknown keys.
    kwargs = {}
    for key, value in block.items():
        if key not in JSON_TO_CASE:
            raise ParseError(f"unknown case key: {key!r}")
        if not isinstance(value, str) or not value:
            raise ParseError(f"{key} must be a non-empty string")
        kwargs[JSON_TO_CASE[key].name.lower()] = value

    # Step 2: require the six standard cases.
    for case in STANDARD_CASES:
        if CASE_TO_JSON[case] not in block:
            raise ParseError(f"missing required case: {CASE_TO_JSON[case]}")

    return Declension(**kwargs)


def validate_file(doc: object) -> tuple[str, list[dict]]:
    """Validate the top-level shape of a parsed JSON document.

    Args:
        doc: The loaded JSON.

    Returns:
        A two-tuple: the category, and the raw entries.

    Raises:
        ParseError: If category or entries is missing, or an extra key
            appears.
    """
    if not isinstance(doc, dict):
        raise ParseError("file must be a JSON object")
    if not isinstance(doc.get("category"), str) or not doc["category"]:
        raise ParseError("file is missing a non-empty 'category'")
    entries = doc.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ParseError("file is missing a non-empty 'entries' list")
    extra = set(doc) - {"category", "entries"}
    if extra:
        raise ParseError(f"unexpected top-level keys: {sorted(extra)}")
    return doc["category"], entries


# =============================================================================
# Reconstructing a NounEntry from database rows
# =============================================================================


def normalize_tree(rows: list[NounRow]) -> NounEntry:
    """Reconstruct a NounEntry from the rows of one tree.

    Partitions the rows into the root, singular children, and plural heads
    with their children, then assembles a NounEntry. If no root row is
    present, the root is found by walking up the parent chain.

    Args:
        rows: The rows of one tree, in any order.

    Returns:
        The reconstructed NounEntry.

    Raises:
        ValueError: If rows is empty or no root is found.
    """
    if not rows:
        raise ValueError("cannot normalize an empty row set")

    # Step 1: index rows by code and find the root.
    by_code = {r.code: r for r in rows}
    root = _find_root(rows, by_code)

    # Step 2: partition non-root rows. Children of the root are singular
    # cases or nominative plurals. Everything else is a plural case, keyed
    # by its parent (a nominative plural).
    singular_rows = []
    plural_heads = []
    plural_children: dict[int, list[NounRow]] = {}
    for row in rows:
        if row.code == root.code:
            continue
        if row.code_parent == root.code:
            if row.plural:
                plural_heads.append(row)
                plural_children.setdefault(row.code, [])
            else:
                singular_rows.append(row)
        else:
            plural_children.setdefault(row.code_parent, []).append(row)

    # Step 3: the dictionary form. For plural-only nouns it is the first
    # plural head's word.
    word = root.word
    if not singular_rows and plural_heads:
        word = plural_heads[0].word

    # Step 4: build singular, if present.
    singular = None
    if singular_rows:
        singular = _declension_from_rows(root, singular_rows)

    # Step 5: build plural, one Declension per nominative plural head.
    plural = None
    if plural_heads:
        plural = tuple(
            _declension_from_rows(h, plural_children.get(h.code, []))
            for h in plural_heads
        )

    # Step 6: assemble. Indeclinable means no children at all.
    return NounEntry(
        word=word,
        gender=root.gender,
        animate=root.animate,
        category=root.category,
        indeclinable=singular is None and plural is None,
        singular=singular,
        plural=plural,
    )


def _find_root(rows: list[NounRow], by_code: dict[int, NounRow]) -> NounRow:
    """Return the root row of a tree.

    Returns a row with code_parent = ROOT_PARENT if one is present.
    Otherwise walks up from any row until reaching one.

    Args:
        rows: The rows to search.
        by_code: The rows indexed by code.

    Returns:
        The root row.

    Raises:
        ValueError: If the chain breaks or a cycle is found.
    """
    for row in rows:
        if row.code_parent == ROOT_PARENT:
            return row

    current = rows[0]
    seen = {current.code}
    while current.code_parent != ROOT_PARENT:
        parent = by_code.get(current.code_parent)
        if parent is None:
            raise ValueError(
                f"parent {current.code_parent} not found; "
                f"database tree is inconsistent"
            )
        if parent.code in seen:
            raise ValueError(f"cycle at code {parent.code}")
        seen.add(parent.code)
        current = parent
    return current


def _declension_from_rows(head: NounRow, children: list[NounRow]) -> Declension:
    """Build a Declension from a head row and its case children.

    The head provides the nominative. Each child provides its case.

    Args:
        head: The nominative row.
        children: The rows for the other cases.

    Returns:
        The Declension.
    """
    kwargs = {"nominative": head.word}
    for child in children:
        if child.case is None:
            continue
        kwargs[child.case.name.lower()] = child.word
    return Declension(**kwargs)


# =============================================================================
# Queries
# =============================================================================


def _word_clause(case_insensitive: bool) -> str:
    """Return the SQL fragment comparing word to %s.

    Case-sensitive form can use the word prefix index. Case-insensitive
    applies LOWER to both sides, which cannot, so it scans.

    Args:
        case_insensitive: Whether to ignore case.

    Returns:
        The comparison fragment.
    """
    if case_insensitive:
        return f"LOWER(word) = LOWER(%s) COLLATE {WORD_COLLATION}"
    return f"word = %s COLLATE {WORD_COLLATION}"


def _query(conn: Connection, sql: str, params: list) -> list[dict]:
    """Run a SELECT and return all rows as dicts."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _query_one(conn: Connection, sql: str, params: list) -> dict | None:
    """Run a SELECT expected to return at most one row."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def _execute(conn: Connection, sql: str, params: list) -> int:
    """Run an INSERT, UPDATE, or DELETE. Returns the affected row count."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def find_roots(
    conn: Connection, table: str, word: str, case_insensitive: bool = False
) -> list[NounRow]:
    """Find root rows whose word matches.

    Roots only (code_parent = 0), so it matches the dictionary form. Used
    by the operations to locate a tree by its dictionary form.

    Args:
        conn: The connection.
        table: The table name.
        word: The word to match.
        case_insensitive: Whether to ignore case.

    Returns:
        The matching root rows.
    """
    sql = (
        f"SELECT {COLUMNS} FROM {table} "
        f"WHERE {_word_clause(case_insensitive)} AND code_parent = {ROOT_PARENT}"
    )
    return [row_from_dict(r) for r in _query(conn, sql, [word])]


def find_trees_for_word(
    conn: Connection, table: str, word: str, case_insensitive: bool = False
) -> list[list[NounRow]]:
    """Find every tree that has a row matching a word, and load each.

    Matches declined forms as well as dictionary forms: searching
    "кроликом" finds the кролик tree.

    Returns one list of rows per matching tree, so the caller can tell
    where one tree ends and the next begins. A word can resolve to more
    than one root (e.g. замок is two distinct nouns), and in that case
    each root's tree is its own element of the returned list.

    Args:
        conn: The connection.
        table: The table name.
        word: The word to match.
        case_insensitive: If True, ignore case in the match. This cannot
            use the word prefix index and scans the table.

    Returns:
        A list of trees. Each tree is a list of NounRow: the root row
        followed by its descendants. Empty if nothing matched.
    """
    # Step 1: find every row whose word matches. These may be non-root
    # rows (declined forms), so each must be walked up to its root.
    sql = f"SELECT {COLUMNS} FROM {table} WHERE {_word_clause(case_insensitive)}"
    matching = [row_from_dict(r) for r in _query(conn, sql, [word])]

    # Step 2: walk each matching row to its root, dedupe roots, and load
    # each tree once. A single tree can produce several matching rows
    # (a word like кролика matches genitive and accusative), which is why
    # the roots are deduped.
    seen_roots = set()
    trees = []
    for row in matching:
        root = find_root_of(conn, table, row)
        if root.code in seen_roots:
            continue
        seen_roots.add(root.code)
        trees.append(load_tree(conn, table, root.code))
    return trees


def load_tree(conn: Connection, table: str, root_code: int) -> list[NounRow]:
    """Load every row of the tree rooted at root_code.

    Assumes root_code is a real root. The tree is shallow: root, children,
    grandchildren.

    Args:
        conn: The connection.
        table: The table name.
        root_code: The root's code.

    Returns:
        The tree's rows, root first.
    """
    # Step 1: the root.
    raw = _query_one(
        conn, f"SELECT {COLUMNS} FROM {table} WHERE code = %s", [root_code]
    )
    if raw is None:
        return []
    root = row_from_dict(raw)

    # Step 2: the root's children.
    children = _children(conn, table, root_code)
    result = [root] + children

    # Step 3: for each nominative plural child, its children.
    for child in children:
        if child.plural:
            result.extend(_children(conn, table, child.code))
    return result


def _children(conn: Connection, table: str, code: int) -> list[NounRow]:
    """Return the direct children of a row."""
    sql = f"SELECT {COLUMNS} FROM {table} WHERE code_parent = %s"
    return [row_from_dict(r) for r in _query(conn, sql, [code])]


def find_root_of(conn: Connection, table: str, row: NounRow) -> NounRow:
    """Walk up from a row to its root.

    Rows that are already roots return without a query.

    Args:
        conn: The connection.
        table: The table name.
        row: The row to start from.

    Returns:
        The root row.

    Raises:
        ValueError: If a parent is missing or a cycle is found.
    """
    if row.code_parent == ROOT_PARENT:
        return row
    current = row
    seen = {current.code}
    while current.code_parent != ROOT_PARENT:
        raw = _query_one(
            conn,
            f"SELECT {COLUMNS} FROM {table} WHERE code = %s",
            [current.code_parent],
        )
        if raw is None:
            raise ValueError(f"parent {current.code_parent} not found")
        if raw["code"] in seen:
            raise ValueError(f"cycle at code {raw['code']}")
        seen.add(raw["code"])
        current = row_from_dict(raw)
    return current


def next_code(conn: Connection, table: str) -> int:
    """Return max(code) + 1, or 1 if the table is empty."""
    row = _query_one(conn, f"SELECT MAX(code) AS m FROM {table}", [])
    m = row["m"] if row and row["m"] is not None else 0
    return m + 1


# =============================================================================
# Tree insertion
# =============================================================================


def insert_tree(conn: Connection, table: str, entry: NounEntry) -> list[int]:
    """Insert a complete tree for an entry.

    Assumes no concurrent writers, so max(code) + 1 is safe. All rows
    share one created_at value, captured once.

    Args:
        conn: The connection.
        table: The table name.
        entry: The entry to insert.

    Returns:
        The codes assigned, root first.
    """
    code = next_code(conn, table)
    now = datetime.now()
    if entry.indeclinable:
        return _insert_indeclinable(conn, table, entry, code, now)
    if entry.singular is None:
        return _insert_plural_only(conn, table, entry, code, now)
    return _insert_standard(conn, table, entry, code, now)


def _insert_indeclinable(conn, table, entry, code, now) -> list[int]:
    """Insert a single row, no children."""
    _insert_row(
        conn,
        table,
        code=code,
        parent=ROOT_PARENT,
        word=entry.word,
        plural=False,
        gender=entry.gender,
        case=None,
        animate=entry.animate,
        category=entry.category,
        created_at=now,
    )
    return [code]


def _insert_plural_only(conn, table, entry, code, now) -> list[int]:
    """Insert a plural-only tree.

    The root is the first nominative plural. Its children are the other
    cases. Additional plural forms become further nominative-plural
    children of the root.
    """
    first = entry.plural[0]
    _insert_row(
        conn,
        table,
        code=code,
        parent=ROOT_PARENT,
        word=first.nominative,
        plural=True,
        gender=None,
        case=Case.NOMINATIVE,
        animate=entry.animate,
        category=entry.category,
        created_at=now,
    )
    inserted = [code]
    next_c = code + 1
    children, next_c = _insert_cases(
        conn,
        table,
        first,
        code,
        next_c,
        plural=True,
        gender=None,
        animate=entry.animate,
        category=entry.category,
        now=now,
    )
    inserted.extend(children)

    for form in entry.plural[1:]:
        head = next_c
        next_c += 1
        _insert_row(
            conn,
            table,
            code=head,
            parent=code,
            word=form.nominative,
            plural=True,
            gender=None,
            case=Case.NOMINATIVE,
            animate=entry.animate,
            category=entry.category,
            created_at=now,
        )
        inserted.append(head)
        children, next_c = _insert_cases(
            conn,
            table,
            form,
            head,
            next_c,
            plural=True,
            gender=None,
            animate=entry.animate,
            category=entry.category,
            now=now,
        )
        inserted.extend(children)
    return inserted


def _insert_standard(conn, table, entry, code, now) -> list[int]:
    """Insert a normal declinable tree.

    Root is nominative singular. Its children are the other singular
    cases and each nominative plural. Plural heads have their own case
    children.
    """
    _insert_row(
        conn,
        table,
        code=code,
        parent=ROOT_PARENT,
        word=entry.singular.nominative,
        plural=False,
        gender=entry.gender,
        case=Case.NOMINATIVE,
        animate=entry.animate,
        category=entry.category,
        created_at=now,
    )
    inserted = [code]
    next_c = code + 1
    children, next_c = _insert_cases(
        conn,
        table,
        entry.singular,
        code,
        next_c,
        plural=False,
        gender=entry.gender,
        animate=entry.animate,
        category=entry.category,
        now=now,
    )
    inserted.extend(children)

    if entry.plural:
        for form in entry.plural:
            head = next_c
            next_c += 1
            _insert_row(
                conn,
                table,
                code=head,
                parent=code,
                word=form.nominative,
                plural=True,
                gender=None,
                case=Case.NOMINATIVE,
                animate=entry.animate,
                category=entry.category,
                created_at=now,
            )
            inserted.append(head)
            children, next_c = _insert_cases(
                conn,
                table,
                form,
                head,
                next_c,
                plural=True,
                gender=None,
                animate=entry.animate,
                category=entry.category,
                now=now,
            )
            inserted.extend(children)
    return inserted


def _insert_cases(
    conn, table, form, parent, next_c, plural, gender, animate, category, now
):
    """Insert the non-nominative cases of a declension as child rows.

    Args:
        conn, table: Connection and table.
        form: The Declension.
        parent: The nominative head's code.
        next_c: The next code to assign.
        plural: True for plural rows.
        gender: Gender for these rows (None for plural).
        animate: Animacy.
        category: Category.
        now: created_at value.

    Returns:
        A two-tuple: the codes assigned, and the next free code.
    """
    codes = []
    for case in Case:
        if case == Case.NOMINATIVE:
            continue
        word = form.get(case)
        if word is None:
            continue
        _insert_row(
            conn,
            table,
            code=next_c,
            parent=parent,
            word=word,
            plural=plural,
            gender=gender,
            case=case,
            animate=animate,
            category=category,
            created_at=now,
        )
        codes.append(next_c)
        next_c += 1
    return codes, next_c


def _insert_row(
    conn, table, code, parent, word, plural, gender, case, animate, category, created_at
) -> None:
    """Insert one row.

    Translates Case to wcase and animate to soul. Sets is_custom = 1.
    created_at is passed in, not NOW(), so all rows of a tree share one
    timestamp.
    """
    sql = (
        f"INSERT INTO {table} "
        f"(word, code, code_parent, plural, gender, wcase, soul, "
        f" is_custom, created_at, category) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)"
    )
    params = [
        word,
        code,
        parent,
        1 if plural else 0,
        gender.value if gender else None,
        case.value if case else None,
        None if animate is None else (1 if animate else 0),
        created_at,
        category,
    ]
    _execute(conn, sql, params)


def delete_tree(conn: Connection, table: str, root_code: int) -> list[NounRow]:
    """Delete the tree rooted at root_code.

    Loads the rows first (to return them), then deletes them by code.

    Args:
        conn: The connection.
        table: The table name.
        root_code: The root's code.

    Returns:
        The rows that were deleted.
    """
    rows = load_tree(conn, table, root_code)
    if not rows:
        return []
    codes = [r.code for r in rows]
    placeholders = ", ".join(["%s"] * len(codes))
    _execute(conn, f"DELETE FROM {table} WHERE code IN ({placeholders})", codes)
    return rows


# =============================================================================
# Operations
# =============================================================================


def op_add(conn, table, entries, logger, case_insensitive=False) -> list[EntryResult]:
    """Insert each entry unless an identical one already exists.

    Matching is on full content: an entry already in the database with the
    same word, gender, animacy, category, and declensions is skipped. A
    root with the same word but different content does not count as a
    match; the entry is inserted as a new tree.

    Args:
        conn: The connection.
        table: The table name.
        entries: The entries to add.
        logger: The logger.
        case_insensitive: If True, match words case-insensitively.

    Returns:
        One EntryResult per entry, in input order.
    """
    results = []
    for entry in entries:
        try:
            # Step 1: For this entry's word, find root rows in the dictionary that
            # match it's spelling (regardless if they have the same declensions)
            #
            # Notes:
            # - A root row is the top of one dictionary entry's tree (code_parent = 0):
            #   (it's dictionary form, e.g. кролик).
            # - An individual word can correspond to multiple roots in the dictionary
            #   (e.g., замок (castle) and замок (lock) have the same spelling and
            #   declensions, but are distinct words in the dictionary due to stress pattern;
            #   and Владимир (city) and Владимир (name) have the same spelling but
            #   different declensions due to animacy, and distinct words in the dictinoary)
            # - find_roots therefore returns a list, not a single row.
            #
            # The return type is list[NounRow]: one NounRow per matching root row.
            # Each is the root alone, not the tree. The children are loaded in
            # Step 2, and only for the roots that matched.
            candidates = find_roots(conn, table, entry.word, case_insensitive)

            # Step 2: For each root row, create a NounEntry from it and compare.
            #
            # Notes:
            # - The == is a full structural comparison: NounEntry and Declension are
            #   dataclasses, so equality checks every field of both, all the way down
            #   (word, gender, animate, category, indeclinable, and every case form in
            #   every declension). It is not a comparison of the word alone.
            # - Doing this after Step 1 (instead of having find_roots return all the
            #   entries), as getting just the root row initially speeds things up
            already = any(
                normalize_tree(load_tree(conn, table, r.code)) == entry
                for r in candidates
            )

            # Step 3: identical content exists. Skip.
            if already:
                result = EntryResult(word=entry.word, outcome=ResultOutcome.SKIPPED)
                results.append(result)
                logger.entry_skipped(f"{entry.word}: already present")
                continue

            # Step 4: insert.
            insert_tree(conn, table, entry)
            conn.commit()
            result = EntryResult(word=entry.word, outcome=ResultOutcome.ADDED)
            results.append(result)
            logger.entry_added(f"{entry.word}: added")
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {exc}")
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {type(exc).__name__}: {exc}")
    return results


def op_update(
    conn,
    table,
    entries,
    logger,
    force_update=False,
    force_upstream=False,
    case_insensitive=False,
) -> list[EntryResult]:
    """Replace the tree for a word, or insert if none exists.

    Matching is on the word alone. Update exists to correct entries that
    are wrong in the database, so the JSON the user supplies is likely to
    differ from what is stored; requiring content to match would make it
    impossible to fix anything. The cost of matching on the word is that
    two distinct entries with the same spelling both match, which is why
    N>1 is refused (with the SQL to remove one) unless --force-update.

    A single match that is an upstream row (is_custom = 0) is also
    refused, because replacing the shared dictionary data is usually a
    mistake; --force-upstream overrides that refusal.

    Args:
        conn: The connection.
        table: The table name.
        entries: The entries to update.
        logger: The logger.
        force_update: If True, allow replacing all matches when N>1.
        force_upstream: If True, allow replacing an upstream row.
        case_insensitive: If True, match words case-insensitively.

    Returns:
        One EntryResult per entry, in input order.
    """
    results = []
    for entry in entries:
        try:
            # Step 1: find roots matching the word. Update matches on the
            # word alone, not on full content, because its purpose is to
            # correct data that is currently wrong in the database.
            roots = find_roots(conn, table, entry.word, case_insensitive)

            # Step: Check for upstream data.
            upstream = [r for r in roots if not r.is_custom]
            if upstream and not force_upstream:
                sql = get_delete_queries(conn, table, roots)
                logger.error(
                    f"{entry.word!r} matches an upstream entry "
                    f"(is_custom = 0); refusing to replace it.\n"
                    f"Pass --force-upstream to replace anyway."
                )
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.ERROR,
                    error=True,
                    message="upstream entry",
                )
                results.append(result)
                continue

            # Step: Multiple roots detected. Refuse unless forced, and show the SQL that
            # would remove each match so the user can act on it.
            if len(roots) > 1 and not force_update:
                sql = get_delete_queries(conn, table, roots)
                logger.error(
                    f"{len(roots)} roots match {entry.word!r}; "
                    f"refusing to update.\n"
                    f"To remove one, run the statement for that tree, then "
                    f"run update again, or pass --force-update to replace "
                    f"all of them:\n{sql}"
                )
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.ERROR,
                    error=True,
                    message=f"{len(roots)} roots match",
                )
                results.append(result)
                continue

            # If every match already has this content, there is nothing to change.
            # This covers N=1 and also the case where several homonyms all match, so
            # a --force-update does not needlessly delete and reinsert identical data.
            if roots and all(
                normalize_tree(load_tree(conn, table, r.code)) == entry for r in roots
            ):
                result = EntryResult(word=entry.word, outcome=ResultOutcome.NO_CHANGE)
                results.append(result)
                logger.entry_skipped(f"{entry.word}: no change")
                continue

            # Step: replace any existing, then update.

            # store what action is being done (update or fresh add), for logging
            action = ResultOutcome.ADDED
            # delete the matches
            if roots:
                for r in roots:
                    delete_tree(conn, table, r.code)
                action = ResultOutcome.UPDATED
            # insert the entry
            insert_tree(conn, table, entry)
            conn.commit()

            result = EntryResult(word=entry.word, outcome=action)
            results.append(result)
            logger.entry_updated(f"{entry.word}: {action}")
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {exc}")
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {type(exc).__name__}: {exc}")
    return results


def op_delete(
    conn, table, entries, logger, force_delete=False, case_insensitive=False
) -> list[EntryResult]:
    """Delete trees whose content matches each entry.

    Matching is on full content. N=0 reports not_found. N=1 deletes. N>1
    refuses unless --force-delete, showing the SQL for each match.

    Args:
        conn: The connection.
        table: The table name.
        entries: The entries to delete.
        logger: The logger.
        force_delete: If True, allow deleting all matches when N>1.
        case_insensitive: If True, match words case-insensitively.

    Returns:
        One EntryResult per entry, in input order.
    """
    results = []
    for entry in entries:
        try:
            # Step 1: find roots with this word, then keep only those
            # whose reconstructed content equals the entry.
            candidates = find_roots(conn, table, entry.word, case_insensitive)
            matches = [
                r
                for r in candidates
                if normalize_tree(load_tree(conn, table, r.code)) == entry
            ]

            # Step 2: no match.
            if not matches:
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.NOT_FOUND,
                    error=True,
                    message="no matching entry",
                )
                results.append(result)
                logger.entry_failed(f"{entry.word}: not found")
                continue

            # Step 3: N>1. Refuse unless forced, and show the SQL.
            if len(matches) > 1 and not force_delete:
                sql = get_delete_queries(conn, table, matches)
                logger.error(
                    f"{len(matches)} entries match {entry.word!r}; "
                    f"refusing to delete.\n"
                    f"To remove one, run the statement for that tree, or "
                    f"pass --force-delete to delete all of them:\n{sql}"
                )
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.ERROR,
                    error=True,
                    message=f"{len(matches)} matches",
                )
                results.append(result)
                continue

            # Step 4: delete every match.
            for r in matches:
                delete_tree(conn, table, r.code)
            conn.commit()
            result = EntryResult(word=entry.word, outcome=ResultOutcome.DELETED)
            results.append(result)
            logger.entry_deleted(f"{entry.word}: deleted")
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {exc}")
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {type(exc).__name__}: {exc}")
    return results


def op_verify(
    conn, table, entries, logger, case_insensitive=False
) -> list[EntryResult]:
    """Check each entry against the database. Read-only.

    Reports matched, mismatched, or not_found. Multiple roots matching the
    same word is a warning.

    Args:
        conn: The connection.
        table: The table name.
        entries: The entries to check.
        logger: The logger.
        case_insensitive: If True, match words case-insensitively.

    Returns:
        One EntryResult per entry, in input order.
    """
    results = []
    for entry in entries:
        try:
            # Step 1: find roots with this word.
            roots = find_roots(conn, table, entry.word, case_insensitive)

            # Step 2: nothing in the database.
            if not roots:
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.NOT_FOUND,
                    error=True,
                    message="not in database",
                )
                results.append(result)
                logger.entry_failed(f"{entry.word}: not found")
                continue

            # Step 3: compare each root's content against the entry.
            matched = any(
                normalize_tree(load_tree(conn, table, r.code)) == entry for r in roots
            )
            if matched:
                multiple = len(roots) > 1
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.MATCHED,
                    warning=multiple,
                    message=(f"{len(roots)} roots for this word" if multiple else None),
                )
                results.append(result)
                logger.entry_matched(f"{entry.word}: matched")
                if multiple:
                    logger.warning(f"{entry.word}: {len(roots)} roots for this word")
            else:
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.MISMATCHED,
                    error=True,
                    message="database content differs",
                )
                results.append(result)
                logger.entry_mismatched(f"{entry.word}: content differs")
        except RumorphError as exc:
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {exc}")
        except Exception as exc:
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            logger.entry_failed(f"{entry.word}: {type(exc).__name__}: {exc}")
    return results


def get_delete_queries(conn, table, roots) -> str:
    """Return DELETE statements, one per tree, for a set of roots.

    Each tree gets a comment naming its word and category (category often
    distinguishes two roots that share a word), then a DELETE statement
    listing every code in the tree.

    Args:
        conn: The connection.
        table: The table name.
        roots: The root rows whose trees to delete.

    Returns:
        A multi-line string of comments and DELETE statements.
    """
    lines = []
    for root in roots:
        tree = load_tree(conn, table, root.code)
        codes = ", ".join(str(r.code) for r in tree)
        label = root.word
        if root.category:
            label += f" ({root.category})"
        lines.append(f"-- {label}")
        lines.append(f"DELETE FROM {table} WHERE code IN ({codes});")
    return "\n".join(lines)


# =============================================================================
# Sanity checks
# =============================================================================


def print_transform_summary(results: list[TransformResult], logger: Logger) -> None:
    """Print the transformation check results under a header.

    Passes get a checkmark, failures get an X. Errors go through
    logger.error, passes through logger.info. The header and rules use
    logger.always so they show regardless of --log-level.

    Args:
        results: The TransformResult objects from the checks.
        logger: The logger.
    """
    rule = "─" * 60
    logger.info(rule)
    logger.info(" SANITY CHECK RESULTS")
    logger.info(rule)
    for r in results:
        if r.error:
            line = f"  ✗ {r.check}"
            if r.message:
                line += f"\n      {r.message}"
            logger.error(line)
        else:
            logger.info(f"  ✓ {r.check}")
    logger.info(rule)


def check_indexes(conn: Connection, table: str):
    """Verify custom indexes present"""

    # Custom indexes set on table
    required_indexes = [("code_idx", None), ("code_parent_idx", None), ("word_idx", 5)]
    sql = (
        "SELECT INDEX_NAME, SUB_PART FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "GROUP BY INDEX_NAME, SUB_PART"
    )
    present = {(r["INDEX_NAME"], r["SUB_PART"]) for r in _query(conn, sql, [table])}

    results = []
    for name, sub in required_indexes:
        # human readable name for this verification test
        # (gets printed in per-run summary)
        result_check = f"Check exists index '{name}'"
        if (name, sub) in present:
            result_outcome = ResultOutcome.TRANSFORMATION_OK
            error = False
        else:
            result_outcome = ResultOutcome.TRANSFORMATION_FAIL
            error = True
        results.append(
            TransformResult(check=result_check, outcome=result_outcome, error=error)
        )
    return results


def check_deti_fix(conn: Connection, table: str):
    """Check дети linked as plural of ребенок"""

    # a test name to display at end of run summary
    result_check = f"Check data fix 'дети' (linked as plural of ребенок)"

    # get root code for ребенок, so can check if its parent of дети
    sql = (
        f"SELECT code FROM {table} "
        f"WHERE word = %s COLLATE {WORD_COLLATION} AND code_parent = 0 LIMIT 1"
    )
    roots = _query(conn, sql, ["ребенок"])
    if not roots:
        # ребенок itself not returning any root which is a separate failure
        return TransformResult(
            check=result_check,
            outcome=ResultOutcome.TRANSFORMATION_FAIL,
            error=True,
            message=f"{result_check}: (no root found for ребенок; so it can not be parent)",
        )
    rebenok_code = roots[0]["code"]

    # Check дети linked as nom plural form
    sql = (
        f"SELECT IID FROM {table} "
        f"WHERE word = %s COLLATE {WORD_COLLATION} "
        f"AND code_parent = %s AND plural = 1 AND wcase = 'им' LIMIT 1"
    )
    if _query(conn, sql, ["дети", rebenok_code]):
        return TransformResult(
            check=result_check, outcome=ResultOutcome.TRANSFORMATION_OK
        )
    else:
        return TransformResult(
            check=result_check,
            outcome=ResultOutcome.TRANSFORMATION_FAIL,
            error=True,
            message=f"{result_check}: дети not им plural of ребенок",
        )


def check_columns(conn: Connection, table: str):
    """Checks required added columns exist"""

    # Required columns and their data types
    required_columns = [
        ("is_custom", "tinyint"),
        ("created_at", "timestamp"),
        ("category", "varchar"),
    ]

    sql = (
        "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
    )

    # Build dict of col name -> data type for each column found
    all_cols = {
        r["COLUMN_NAME"]: r["DATA_TYPE"].lower() for r in _query(conn, sql, [table])
    }

    # Loop through each required column, verifiy it exists and is the correct type
    results = []
    for required_col, required_type in required_columns:
        # human readable test name for per-run summary
        check = f"Check column {required_col} exists with type {required_type}"

        # From the hash of all cols found in db, get
        # datatype of required col (if not found, fallback to "")
        actual_type = all_cols.get(required_col, "")

        if actual_type:
            # required col was found in db query; verify correct type
            if required_type == actual_type:
                results.append(
                    TransformResult(
                        check=check,
                        outcome=ResultOutcome.TRANSFORMATION_OK,
                        error=False,
                        message=None,
                    )
                )
            else:
                # col exists but type is incorrect
                message = (
                    f"Col {required_col} exists in table {table} with type {actual_type!r}, "
                    f"expected {required_type!r}"
                )
                results.append(
                    TransformResult(
                        check=check,
                        outcome=ResultOutcome.TRANSFORMATION_FAIL,
                        error=True,
                        message=message,
                    )
                )
        else:
            # there was no result for this col in the database
            results.append(
                TransformResult(
                    check=check,
                    outcome=ResultOutcome.TRANSFORMATION_FAIL,
                    error=True,
                    message=f"column {required_col} is missing from {table}",
                )
            )
    return results


def check_transformations(
    conn: Connection, table: str, logger: Logger
) -> list[TransformResult]:
    """Verify the setup transformations are present.

    Checks that the three indexes and three custom columns exist, and
    that the дети data fix has been applied. Prints the results and
    returns them.

    Args:
        conn: The connection.
        table: The table name.
        logger: For reporting each check's result.

    Returns:
        The list of TransformResult objects for the checks.
    """
    results = []

    results.extend(check_indexes(conn, table))
    results.extend(check_columns(conn, table))
    results.append(check_deti_fix(conn, table))

    print_transform_summary(results, logger)
    return results


# =============================================================================
# Bare --word search
# =============================================================================


def boxed(text: str, pad: int = 0) -> str:
    """Return text inside a simple ASCII box.

    Args:
        text: The text to box.

    Returns:
        A three-line string: top border, text line, bottom border.
    """
    inner = len(text) + 2
    top = "+" + "-" * inner + "+"
    lines = [top, f"| {text} |", top]
    # add padding in front
    lines = [f"{' '*pad}{line}" for line in lines]
    return "\n".join(lines)


def word_search(
    conn: Connection, table: str, word: str, case_insensitive: bool, logger: Logger
) -> None:
    """Look up a word and print each matching tree as a table.

    A word can resolve to more than one root (замок is two distinct
    nouns). Each tree is printed separately, with a header between them
    when there is more than one.

    Args:
        conn: The connection.
        table: The table name.
        word: The word to look up. Declined forms match their entry.
        case_insensitive: If True, ignore case in the match.
        logger: The logger to print through.
    """
    # color to use for the tree
    color = Colors.BRIGHT_YELLOW

    # Step 1: load every tree containing a row that matches the word.
    trees = find_trees_for_word(conn, table, word, case_insensitive)

    # Step 2: report if nothing matched.
    word_header = boxed(word, pad=40)
    logger.always(f"\n{word_header}\n", Colors.BOLD_BLUE)
    if not trees:
        logger.always("  (no matches)", color)
        return

    # Step 3: print one table per tree, with a header between them when
    # there is more than one match.
    for i, tree in enumerate(trees, start=1):
        if len(trees) > 1:
            logger.always(f"\n  match {i}:", Colors.RED)
        logger.always(get_tree_table(tree), color)


def get_tree_table(tree: list[NounRow]) -> str:
    """Format the rows of one tree as a fixed-width table.

    Column widths are computed from the data; no column is truncated.
    The result is a single string with embedded newlines, suitable for
    handing to the logger as one block.

    Args:
        tree: The rows of one tree, as returned by load_tree.

    Returns:
        The table as one string.
    """
    # Step 1: the column headers, in display order.
    headers = [
        "IID",
        "word",
        "code",
        "parent",
        "pl",
        "gender",
        "case",
        "animate",
        "custom",
        "category",
        "created_at",
    ]

    # Step 2: render each row to a list of cell strings. Nullable columns
    # render as "NULL"; the two boolean columns render as "1", "0", or
    # "" when unset (pl) and "NULL" when unset (animate).
    data = [
        [
            str(r.IID),
            r.word,
            str(r.code),
            str(r.code_parent),
            "1" if r.plural else "0" if r.plural is not None else "",
            r.gender.value if r.gender else "NULL",
            r.case.value if r.case else "NULL",
            "1" if r.animate else "0" if r.animate is not None else "NULL",
            "1" if r.is_custom else "0",
            r.category or "NULL",
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "NULL",
        ]
        for r in tree
    ]

    # Step 3: compute each column's width from its header and cells.
    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Step 4: build the header row, separator row, and data rows.
    lines = [
        "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  " + "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in data:
        lines.append("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))

    # Step 5: join into a single string.
    return "\n".join(lines)


# =============================================================================
# Output helpers
# =============================================================================


RESULT_SYMBOLS = {
    ResultOutcome.ADDED: ("+", Colors.GREEN),
    ResultOutcome.UPDATED: ("~", Colors.YELLOW),
    ResultOutcome.DELETED: ("-", Colors.RED),
    ResultOutcome.SKIPPED: ("x", Colors.DIM),
    ResultOutcome.MATCHED: ("✓", Colors.CYAN),
    ResultOutcome.MISMATCHED: ("✗", Colors.BRIGHT_RED),
    ResultOutcome.NOT_FOUND: ("✗", Colors.BRIGHT_RED),
    ResultOutcome.NO_CHANGE: ("x", Colors.DIM),
}


def print_result(logger: Logger, r: EntryResult) -> None:
    """Print one entry's result.

    Errors get '!', warnings get '?', successes get the outcome's symbol.

    Args:
        logger: The logger.
        r: The result.
    """
    if r.error:
        logger.info(f"  ! {r.word}: {r.message}", Colors.BRIGHT_RED)
    elif r.warning:
        logger.info(f"  ? {r.word}: {r.message}", Colors.BRIGHT_YELLOW)
    else:
        symbol, code = RESULT_SYMBOLS.get(r.outcome, ("?", Colors.DIM))
        logger.info(f"  {symbol} {r.word}: {r.outcome}", code)


# Delimiters for the four summary levels. Run is heaviest, file is
# lightest, so nesting is visible at a glance.
_RUN_RULE = "═" * 60
_OP_RULE = "─" * 60
_FILE_RULE = "─" * 40


def build_entry_lines(results: list[EntryResult], indent: str = "    ") -> str:
    """Render one line per entry result.

    Reuses the symbol table from RESULT_SYMBOLS. Errors and warnings get
    their own prefix, matching print_result.

    Args:
        results: The EntryResult objects for one file.
        indent: Leading whitespace for every line.

    Returns:
        One line per result, newline-joined. Empty string if no results.
    """
    lines = []
    for r in results:
        if r.error:
            symbol = "!"
            detail = r.message or r.outcome
        elif r.warning:
            symbol = "?"
            detail = r.message or r.outcome
        else:
            symbol, _ = RESULT_SYMBOLS.get(r.outcome, ("?", Colors.DIM))
            detail = r.outcome
        lines.append(f"{indent}{symbol} {r.word}: {detail}")
    return "\n".join(lines)


def build_file_summary(
    path, category: str, operation: str, results: list[EntryResult], index: int
) -> str:
    """Render the summary block for one file.

    Args:
        path: The file's path.
        category: The file-level category, or "" if unknown.
        operation: The operation name.
        results: The EntryResult objects for this file.
        index: 1-based index of the file within the run.

    Returns:
        The file summary as a string.
    """
    counts = Counts()
    for r in results:
        counts.add_result(r)

    lines = [
        _FILE_RULE,
        f" FILE #{index}",
        _FILE_RULE,
        f"   Path:      {path}",
        f"   Operation: {operation}",
        f"   Category:  {category or 'NULL'}",
        f"   Result:    {counts.summary()}",
        _FILE_RULE,
    ]
    entries = build_entry_lines(results)
    if entries:
        lines.append(entries)
    return "\n".join(lines)


def build_operation_summary(
    operation: str, file_summaries: list[str], file_paths: list, totals: Counts
) -> str:
    """Render the summary block for one operation.

    Args:
        operation: The operation name.
        file_summaries: The already-built file summary strings.
        file_paths: The file paths, in order, for the file list.
        totals: Aggregated counts across all files in this operation.

    Returns:
        The operation summary as a string.
    """
    lines = [
        _OP_RULE,
        f" OPERATION: {operation}",
        _OP_RULE,
        "   Files:",
    ]
    for p in file_paths:
        lines.append(f"     - {p}")
    lines.append(f"   Result: {totals.summary()}")
    lines.append(_OP_RULE)

    for i, summary in enumerate(file_summaries, start=1):
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


def build_run_summary(
    operation: str,
    file_summaries: list[str],
    file_paths: list,
    totals: Counts,
    entry_count: int,
) -> str:
    """Render the top-level run summary, including all nested sections.

    Args:
        operation: The operation name.
        file_summaries: The already-built file summary strings.
        file_paths: The file paths, in order.
        totals: Aggregated counts across the whole run.
        entry_count: Total entries processed.

    Returns:
        The run summary as a string.
    """
    lines = [
        _RUN_RULE,
        " RUN SUMMARY",
        _RUN_RULE,
        f"   Operation:       {operation}",
        f"   Files processed: {len(file_paths)}",
        f"   Entries:         {entry_count}",
        f"   Result:          {totals.summary()}",
        _RUN_RULE,
        "",
        build_operation_summary(operation, file_summaries, file_paths, totals),
    ]
    return "\n".join(lines)


def build_problems_block(title: str, problems: list) -> str:
    if not problems:
        return ""
    rule = "═" * 60
    lines = [rule, f" {title}", rule]
    for r in problems:
        lines.append(f"   {r.label}: {r.message or r.outcome}")
    lines.append(rule)
    return "\n".join(lines)


def print_problems(errors: list, warnings: list, logger: Logger) -> None:
    """Print the ERRORS and WARNINGS blocks, if either is non-empty.

    Args:
        errors: Results with .error set.
        warnings: Results with .warning set.
        logger: The logger.
    """
    if errors:
        logger.error(build_problems_block("ERRORS", errors))
    if warnings:
        logger.warning(build_problems_block("WARNINGS", warnings))


# =============================================================================
# CLI
# =============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the parser and parse argv.

    Two subcommands: entries and util. Common flags are attached to both
    via a parent parser.

    Args:
        argv: Argument list, or None for sys.argv[1:].

    Returns:
        The parsed Namespace.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path, default=None, help="Path to config file."
    )
    common.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Colorize output.",
    )
    common.add_argument(
        "--stderr", action="store_true", help="Send all output to stderr."
    )
    common.add_argument(
        "--log-level",
        choices=Logger.get_levels(),
        default="info",
        metavar="LEVEL",
        help=(
            f"Minimum severity of messages to show. One of {Logger.get_levels()}. "
            f"Operation shows per-entry output but not diagnostics."
        ),
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        help="Shortcut for --log-level DEBUG.",
    )
    common.add_argument(
        "--quiet",
        action="store_true",
        help="Shortcut for --log-level ERROR.",
    )
    common.add_argument(
        "--case-insensitive",
        action="store_true",
        help="Ignore case in word matches (slower).",
    )
    common.add_argument(
        "--version", action="store_true", help="Print version and exit."
    )

    parser = argparse.ArgumentParser(prog="rumorph", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    ap = sub.add_parser("entries", parents=[common], help="Operations on JSON files.")
    ap.add_argument("path", type=Path, help="JSON file or directory.")
    ap.add_argument(
        "operation",
        choices=[
            "add",
            "update",
            "delete",
            "verify",
            "verify-all",
            "validate-json",
        ],
    )
    ap.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories.",
    )
    ap.add_argument(
        "--force-update",
        action="store_true",
        help="Replace all matches when update is ambiguous.",
    )
    ap.add_argument(
        "--force-upstream", action="store_true", help="Replace upstream entries."
    )
    ap.add_argument(
        "--force-delete",
        action="store_true",
        help="Delete all matches when delete is ambiguous.",
    )

    ut = sub.add_parser("util", parents=[common], help="Standalone operations.")
    ut.add_argument("--word", help="Look up a word and print its rows.")
    ut.add_argument("--sanity", action="store_true", help="Run sanity checks.")

    return parser.parse_args(argv)


def use_color(mode: str, stream) -> bool:
    """Decide whether to emit color.

    Args:
        mode: "auto", "always", or "never".
        stream: The output stream.

    Returns:
        True to use color.
    """
    if mode == "always":
        return True
    if mode == "never":
        return False
    return hasattr(stream, "isatty") and stream.isatty()


def resolve_log_level(args: argparse.Namespace) -> str:
    """Determine the effective log level from the parsed arguments.

    The arguments --verbose, --quiet, and --log-level are mutually
    exclusive, so at most one is set (or none, in which case the default
    applies). This function returns the chosen level as a string matching
    the Logger.LEVELS key names.

    Args:
        args: The parsed arguments.

    Returns:
        The chosen log level, as one of the Logger.LEVELS key strings.
    """
    if args.verbose:
        return "debug"
    if args.quiet:
        return "error"
    if args.log_level:
        return args.log_level
    return "info"


def main(argv: list[str] | None = None) -> int:
    """Parse args, load config, dispatch. Returns the exit code.

    Args:
        argv: Argument list, or None for sys.argv[1:].

    Returns:
        The process exit code.
    """
    args = parse_args(argv)

    # Build the logger. This happens before anything that could
    # produce output, including --version.
    stream = sys.stderr if args.stderr else sys.stdout
    logger = Logger(
        level=resolve_log_level(args),
        stream=stream,
        use_color=use_color(args.color, stream),
    )
    # _test_logger(logger)

    # --version exits before config loading.
    if args.version:
        logger.always(f"rumorph {VERSION}")
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error(f"{exc}")
        return 1

    # open the database connection
    try:
        conn = connect(config)
    except DbError as exc:
        logger.error(f"Database error during run:\n{exc}\n{traceback.format_exc()}")
        return 1

    try:
        if args.command == "util":
            results = _run_util(args, config, conn, logger)
        elif args.command == "entries":
            results = _run_entries(args, config, conn, logger)
        else:
            raise RumorphError(f"unknown subcommand: {args.command!r}")
    finally:
        conn.close()

    errors = [r for r in results if r.error]
    warnings = [r for r in results if r.warning]

    print_problems(errors, warnings, logger)

    if errors:
        return 1
    # don't return 1 on warnings because
    # they are not true errors and don't
    # want to crash CI
    return 0


def _run_util(args, config: dict, conn: Connection, logger: Logger) -> list[Result]:
    """Handle util: --word, --sanity

    Runs each requested operation against the provided connection,
    returns the collected results.

    Args:
        args: Parsed arguments.
        config: The [mysql] dict.
        conn: The database connection.
        logger: The logger.

    Returns:
        The list of Result objects produced by the requested operations.
    """
    results = []
    if args.sanity:
        results.extend(check_transformations(conn, config["table"], logger))
    if args.word:
        # no results to return
        word_search(conn, config["table"], args.word, args.case_insensitive, logger)
    return results


def _run_entries(args, config: dict, conn: Connection, logger: Logger) -> list[Result]:
    """Handle entries subcommand: run one operation over one or more JSON files.

    verify-all is a whole-database check, so it runs after the file
    loop, in addition to processing the files. Everything else processes
    files only.

    Args:
        args: Parsed arguments.
        config: The [mysql] dict.
        conn: The database connection.
        logger: The logger.

    Returns:
        The list of Result objects produced by the operation.
    """
    results = []

    # Step 1: find files.
    files = _discover(args.path, recursive=not args.no_recursive)
    if not files:
        results.append(
            Result(
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"no JSON files at {args.path}",
            )
        )

    # Step 2: run the operation over every file, collecting what the
    # end-of-run summary needs: the results per file, and the totals.
    totals = Counts()
    file_summaries = []

    for path in files:
        logger.info(f"\n─── {path} ───")
        counts, entry_results, category = _process_file(
            args, config, conn, path, logger
        )
        results.extend(entry_results)
        totals.add_counts(counts)
        logger.info(f"\n  {counts.summary()}")
        file_summaries.append(
            build_file_summary(
                path, category, args.operation, results, len(file_summaries) + 1
            )
        )

    # Step 3: If verify-all supplied, runs final sanity/transformation check
    if args.operation == "verify-all":
        results.extend(check_transformations(conn, config["table"], logger))

    # Step 4: build the run summary from what was collected, and print it
    # once.
    logger.info(
        build_run_summary(
            operation=args.operation,
            file_summaries=file_summaries,
            file_paths=files,
            totals=totals,
            entry_count=len(results),
        )
    )
    logger.info(f"TOTAL: {totals.summary()}")

    return results


def _process_file(
    args, config: dict, conn: Connection, path: Path, logger: Logger
) -> tuple[Counts, list, str]:
    """Run the requested operation on one file.

    Returns a three-tuple: counts for the file, the list of EntryResult
    for the file, and the file's category.

    Args:
        args: Parsed arguments.
        config: The [mysql] dict.
        conn: The database connection.
        path: The file to process.
        logger: The logger.

    Returns:
        A three-tuple of (Counts, list[EntryResult], category).
    """
    counts = Counts()

    # Step 1: read and validate file structure.
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        category, raw_entries = validate_file(doc)
    except (OSError, json.JSONDecodeError, ParseError) as exc:
        logger.error(f"  ! {path}: {exc}")
        return counts, [], ""

    # Step 2: parse entries. Parse failures are reported and excluded.
    entries = []
    for raw in raw_entries:
        try:
            entries.append(parse_entry(raw, category))
        except ParseError as exc:
            word = raw.get("word", "?")
            logger.error(f"  ! {word}: {exc}")
            counts.failed += 1

    # Step 3: validate-json does not touch the database.
    if args.operation == "validate-json":
        results = []
        for entry in entries:
            counts.matched += 1
            logger.info(f"  ✓ {entry.word}: valid", Colors.CYAN)
        return counts, results, category

    # Run the operation
    ci = args.case_insensitive
    table = config["table"]
    if args.operation == "add":
        results = op_add(conn, table, entries, logger, ci)
    elif args.operation == "update":
        results = op_update(
            conn, table, entries, logger, args.force_update, args.force_upstream, ci
        )
    elif args.operation == "delete":
        results = op_delete(conn, table, entries, logger, args.force_delete, ci)
    elif args.operation in ["verify", "verify-all"]:
        results = op_verify(conn, table, entries, logger, ci)
    else:
        results = []

    # Step 5: print results and tally.
    for r in results:
        # print_result(logger, r)
        counts.add_result(r)

    return counts, results, category


def _discover(path: Path, recursive: bool) -> list[Path]:
    """Find JSON files under a path.

    Args:
        path: A file or directory.
        recursive: Descend into subdirectories when True.

    Returns:
        The files, sorted.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    pattern = "**/*.json" if recursive else "*.json"
    return sorted(path.glob(pattern))


def _test_logger(logger: Logger) -> None:
    test = "Тест"
    logger.debug(test)
    logger.info(test)
    logger.warning(test)
    logger.error(test)
    logger.critical(test)


if __name__ == "__main__":
    sys.exit(main())
