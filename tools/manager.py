#!/usr/bin/env python3
"""rumorph: manage custom Russian noun entries in the runouns database.

Reads JSON files describing Russian nouns and adds, updates, deletes, or
verifies the matching rows in the nouns_morf MySQL table. Also provides a
word lookup and a database sanity check.

USAGE

    python manager.py entries <path> <operation> [flags]
    python manager.py json validate [flags]
    python manager.py util --word <word> [flags]
    python manager.py util --sanity [flags]

    Operations for entries: add, update, delete, verify, verify-all

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

validate json
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
from dataclasses import dataclass, field, fields
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
# Basic JSON validation setup (dependency free)
# =============================================================================


@dataclass(frozen=True)
class FieldSpec:
    """Expected type and optional constraints for one JSON key."""

    type: type
    enum: type[Enum] | None = None
    allow_empty: bool = True


# Top-level keys of a JSON file.
REQUIRED_FILE_FIELDS = (
    ("category", FieldSpec(type=str, allow_empty=False)),
    ("entries", FieldSpec(type=list)),
)

# Required top-level keys for individual entries
# in the "entries" list
REQUIRED_ENTRY_FIELDS = (
    ("word", FieldSpec(type=str, allow_empty=False)),
    ("gender", FieldSpec(type=str, enum=Gender)),
    ("animate", FieldSpec(type=bool)),
)

# Optional top-level keys for individual entries
# in the "entries" list.
OPTIONAL_ENTRY_FIELDS = (
    ("indeclinable", FieldSpec(type=bool)),
    ("singular", FieldSpec(type=dict)),
    ("plural", FieldSpec(type=(dict, list))),
)

# Specs for declensions (dics in "singular"
# and "plural" blocks)

# Every case name, as JSON keys with string values.
DECLENSION_FIELDS = {
    CASE_TO_JSON[case]: FieldSpec(type=str, allow_empty=False) for case in Case
}
REQUIRED_DECLENSION_FIELDS = tuple(
    (CASE_TO_JSON[case], DECLENSION_FIELDS[CASE_TO_JSON[case]])
    for case in STANDARD_CASES
)
OPTIONAL_DECLENSION_FIELDS = tuple(
    (CASE_TO_JSON[case], DECLENSION_FIELDS[CASE_TO_JSON[case]])
    for case in Case
    if case not in STANDARD_CASES
)


def check_fields(
    raw: dict,
    required: tuple | None = None,
    optional: tuple | None = None,
    no_extra: bool = False,
) -> list[str]:
    """Validate a dict against field specs.

    Checks required keys (present and correctly typed), optional keys
    (correctly typed when present), and optionally rejects any key not
    named in either list. Returns one message per problem found; does
    not stop at the first.

    Args:
        raw (dict): The dict to check.
        required (tuple | None): An iterable of (key, FieldSpec) pairs
            that must be present.
        optional (tuple | None): An iterable of (key, FieldSpec) pairs
            that may be present.
        no_extra (bool): If True, any key in raw that is not in required
            or optional is an error.

    Returns:
        list[str]: One message per problem found. Empty when clean.
    """
    errors = []
    required = required or ()
    optional = optional or ()

    for key, spec in required:
        if key not in raw:
            errors.append(f"missing required key '{key}'")
            continue
        errors.extend(_check_value(key, raw[key], spec))

    for key, spec in optional:
        if key not in raw:
            continue
        errors.extend(_check_value(key, raw[key], spec))

    if no_extra:
        allowed = {key for key, _ in required} | {key for key, _ in optional}
        extra = set(raw) - allowed
        if extra:
            sorted_display = ", ".join([f"'{e}'" for e in sorted(extra)])
            errors.append(f"unexpected keys: {sorted_display}")

    return errors


def _check_value(key: str, value, spec: FieldSpec) -> list[str]:
    """Check one value against a FieldSpec.

    Runs three checks, in order, and stops early if the type is wrong:
    the value must be the expected type, must be in the spec's enum when
    one is set, and must be non-empty when the spec forbids empty
    strings. Returns one message per problem found.

    Args:
        key (str): The key, used in error messages.
        value: The value to check.
        spec (FieldSpec): The expected type and constraints.

    Returns:
        list[str]: One message per problem found. Empty when clean.
    """
    errors = []

    # Check 1: the value must be the expected type. If it isn't, the
    # checks below don't apply, since they assume the value is the right
    # kind of thing.
    if not isinstance(value, spec.type):
        expected = (
            spec.type.__name__
            if isinstance(spec.type, type)
            else " or ".join(t.__name__ for t in spec.type)
        )
        errors.append(f"'{key}' must be {expected}, got {type(value).__name__}")
        return errors

    # Check 2: when the spec names an enum, the value must be one of its
    # members' values.
    if spec.enum is not None and value not in (m.value for m in spec.enum):
        valid = [m.value for m in spec.enum]
        errors.append(f"'{key}' must be one of {valid}, got {value!r}")

    # Check 3: an empty string is only an error when the spec forbids it.
    if isinstance(value, str) and not spec.allow_empty and not value:
        errors.append(f"'{key}' must be a non-empty string")

    return errors


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

    def operation(self, text: str, code: str = None, end="\n") -> None:
        self._write(text, level="operation", code=code, end=end, stream=sys.stdout)

    def warning(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.BRIGHT_YELLOW
        self._write(text, level="warn", code=color, end=end, stream=sys.stderr)

    def error(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.BRIGHT_RED
        self._write(text, level="error", code=color, end=end, stream=sys.stderr)

    def critical(self, text: str, code: str = None, end="\n") -> None:
        color = code if code is not None else Colors.URGENT_RED
        self._write(text, level="critical", code=color, end=end, stream=sys.stderr)

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
# Data types - JSON parsing
# =============================================================================


@dataclass
class JsonFile:
    """One JSON file after loading, validating, and parsing.

    Holds whatever could be read from the file. When errors is empty,
    entries contains every entry in the file and all are valid. When
    errors is non-empty, entries may be empty or partial and the
    messages in errors describe what went wrong, at either the
    file level or the entry level.

    Attributes:
        path (Path): The source file.
        category (str): The file-level category, or "NULL" if missing.
        entries (list[NounEntry]): The entries that parsed.
        errors (list[str]): One formatted message per problem found.
            Falsy when the file is clean, so `if json_file.errors:`
            reads as a check for malformed files.
    """

    path: Path
    category: str = "NULL"
    entries: list[NounEntry] = field(default_factory=list)

    # a list of errors encountered.
    # For general file errors, one string each.
    # Errors within entry validation are one per entry.
    errors: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class JsonEntry:
    """Base class for a parsed JSON entry, valid or malformed.

    Exists only to give parse_entries a single return type. It carries
    no data of its own; the shared data between the two subclasses is
    nothing, and their fields are entirely different. What it provides
    is the malformed check, so a caller holding a bare JsonEntry can
    tell which subclass it has without importing both, and a no-op
    summary so either subclass can define its own rendering without a
    base implementation to call.
    """

    @property
    def malformed(self) -> bool:
        """Return True if this is a MalformedEntry"""
        return isinstance(self, MalformedEntry)

    @property
    def summary(self) -> str:
        """Render this entry for display. Subclasses must override."""

        # intentionally raise if this is not implemented on a subclass.
        # JsonEntry is a general base only intended so that parse_entries and callers
        # can operate on a single class instead of having to do isinstance checks.
        # if one of the subclasses did not implement and the base class is hit,
        # this is an issue with the tool and fail fast so I can fix it.
        raise NotImplementedError(f"{type(self).__name__} must define summary")


@dataclass(kw_only=True)
class NounEntry(JsonEntry):
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

    @property
    def summary(self) -> str:
        """Render this entry as a multi-line string for display.

        Shows the parsed fields, then the base rendering (source file
        and raw JSON).

        Returns:
            str: The rendered entry, without a trailing newline.
        """
        fields = "\n".join(
            [
                f"  • word: {self.word}",
                f"  • gender: {self.gender.value}",
                f"  • animate: {self.animate}",
                f"  • category: {self.category}",
                f"  • indeclinable: {self.indeclinable}",
            ]
        )
        return f"parsed:\n{fields}"


@dataclass(kw_only=True)
class MalformedEntry(JsonEntry):
    """An entry that failed to parse, with the problems found.

    Produced by parse_entry when any check failed. Holds every problem
    found (parsing does not stop at the first) and the word when it
    could be read, so the rendered summary identifies which entry in the
    file the problems belong to.

    Attributes:
        word (str): The entry's word, or a placeholder when the word
            could not be parsed. Used to identify the entry in output.
        errors (list[str]): One message per problem found. Never empty;
            an entry with no problems is a NounEntry instead.
    """

    word: str = "Unknown (could not be parsed)"
    errors: list[str]

    @property
    def summary(self) -> str:
        """Render this entry as a multi-line string for display.

        Leads with the problems, then shows the base rendering (source
        file and raw JSON).

        Returns:
            str: The rendered entry, without a trailing newline.
        """
        lines = [f"Entry '{self.word}' errors:"]
        lines.extend([f"  • {e}" for e in self.errors])
        return "\n".join(lines)


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


# =============================================================================
# Data types - Database
# =============================================================================


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


# =============================================================================
# Data types - Run Result Outcomes
# =============================================================================


@dataclass(frozen=True)
class ResultOutcomeHolder:
    """Display attributes for one ResultOutcome member.

    Carries the summary name (the Counts field name and the text used in
    printed output), the symbol (the leading character in per-entry
    lines), and the ANSI color code for that line.
    """

    summary: str
    symbol: str
    code: str

    def __str__(self):
        return f"{self.symbol} {self.summary}"


class ResultOutcome(Enum):
    """Identifiers for the result state of a single operation or check.

    These are not semantic values. Each member is an identifier that
    also carries the three pieces of display information the summary
    printers need: a summary name (used as the Counts field name and in
    printed output), a symbol (the leading character in per-entry lines),
    and a color code (the ANSI escape for that line).

    Members are ResultOutcomeHolder instances; the summary, symbol, and
    code properties expose their fields. Nothing outside the summary
    printing and counting code depends on these values.
    """

    # A JSON file had errors
    JSON_MALFORMED = ResultOutcomeHolder(summary="errored", symbol="x", code=Colors.RED)

    # A JSON file passed validation and could be successfully parsed.
    JSON_PASSED = ResultOutcomeHolder(
        summary="validated", symbol="✓", code=Colors.GREEN
    )

    ADDED = ResultOutcomeHolder(summary="added", symbol="+", code=Colors.GREEN)

    UPDATED: ResultOutcomeHolder(summary="updated", symbol="~", code=Colors.YELLOW)

    DELETED = ResultOutcomeHolder(summary="deleted", symbol="-", code=Colors.RED)

    SKIPPED = ResultOutcomeHolder(summary="skipped", symbol="x", code=Colors.DIM)

    # A verify found exactly one matching tree. This is the clean case.
    MATCHED = ResultOutcomeHolder(summary="matched", symbol="✓", code=Colors.CYAN)

    # A verify found more than one matching tree for the same word, so the
    # result is ambiguous and the user needs to decide which one they mean.
    MATCHED_AMBIGUOUS = ResultOutcomeHolder(
        summary="matched_ambiguous", symbol="!", code=Colors.BRIGHT_YELLOW
    )

    COMPLETE = ResultOutcomeHolder(summary="complete", symbol="=", code=Colors.GREEN)

    MISMATCHED = ResultOutcomeHolder(
        summary="mismatched", symbol="✗", code=Colors.BRIGHT_RED
    )

    NOT_FOUND = ResultOutcomeHolder(
        summary="not_found", symbol="✗", code=Colors.BRIGHT_RED
    )

    NO_CHANGE = ResultOutcomeHolder(summary="no_change", symbol="x", code=Colors.DIM)

    JSON_VALIDATED = ResultOutcomeHolder(
        summary="json_validated", symbol="✓", code=Colors.CYAN
    )

    ERROR = ResultOutcomeHolder(summary="error", symbol="✗", code=Colors.BRIGHT_RED)

    TRANSFORMATION_OK = ResultOutcomeHolder(
        summary="transformation_ok", symbol="✓", code=Colors.GREEN
    )

    TRANSFORMATION_FAIL = ResultOutcomeHolder(
        summary="transformation_fail", symbol="✗", code=Colors.BRIGHT_RED
    )

    @property
    def summary(self) -> str:
        return self.value.summary

    @property
    def symbol(self) -> str:
        return self.value.symbol

    @property
    def code(self) -> str:
        return self.value.code


# =============================================================================
# Data types - Run Results
# =============================================================================


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
        return self.outcome.summary

    @property
    def summary_line(self) -> str:
        """One-line summary of this result, for display."""
        return f"{self.outcome.symbol}: {self.message or self.outcome.summary}"

    def print_result_summary(self, logger: Logger, indent: str = "    ") -> None:
        """Write this result's summary_line through the Logger.

        Routes to logger.error, logger.warning, or logger.operation based on
        the result's flags: error=True logs at error level, warning=True
        logs at warning level, otherwise operation level with the outcome's
        color. The caller supplies the logger so the destination and
        color settings stay with the caller, not the result.

        Args:
            logger (Logger): The logger to write through.

        Returns:
            None
        """
        summary = f"{indent}{self.summary_line}"
        if self.error:
            logger.error(summary)
        elif self.warning:
            logger.warning(summary)
        else:
            logger.operation(summary, self.outcome.code)


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

    @property
    def summary_line(self) -> str:
        """One-line summary of this entry result, for display."""
        return (
            f"{self.outcome.symbol} {self.word}: {self.message or self.outcome.summary}"
        )


@dataclass(kw_only=True)
class FileResult(Result):
    """The outcome of one operation across one file.

    Attributes:
        file (Path): Path to the file that was processed.
        category (str): The file-level category, or "" if the file could
            not be read or parsed.
        entries (list[EntryResult]): One EntryResult per noun entry in
            the file. Empty if the file failed before any entry was
            processed.
        counts (Counts): A Counts computed from entries in
            __post_init__. Not an init argument; derived from entries.
    """

    file: Path
    # entries is optional - for example, error case where entries
    # can't be parsed but still need to store a FileResult for summary
    # For the default value, use default_factory, not = []: Python
    # evaluates a default value once, at class definition, so = [] would
    # give every FileResult the same list object and appending to one would
    # append to all. The factory runs per instance instead, so each FileResult gets its own list.
    entries: list[EntryResult] = field(default_factory=list)
    category: str = "NULL"
    kind: str = "file"

    @property
    def label(self) -> Path:
        return self.file

    # store a Counts object for this FileResult.
    # once populated with self.entries, counts
    # about the file will be available to callers e.g.,
    #    fileResult.counts.summary
    #    fileResult.counts.count
    counts: Counts = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """
        Populates the Counts object after __init__ with the
        entries for this file.
        """
        self.counts = Counts()
        self.counts.add_results(self.entries)


@dataclass(kw_only=True)
class JsonValidationResult(Result):
    """JSON validation on a JSON file."""

    # human readable summary of the check
    path: Path

    # JsonFile the result is based on.
    # includes list of parsed NounEntry objects,
    # error strings encountered, etc.
    file: JsonFile

    kind: str = "json-validation"

    @property
    def label(self) -> str:
        return self.path

    @property
    def summary_line(self) -> str:
        """Summary of this JSON validation result, for display."""
        base = f"{self.outcome.symbol} {self.path.name}: {self.outcome.summary}"
        if self.message:
            base += f"\n{self.message}"
        return base


@dataclass(kw_only=True)
class TransformResult(Result):
    """The outcome of one sanity check."""

    # human readable summary of the check
    check: str
    kind: str = "check"

    @property
    def label(self) -> str:
        return self.check


# =============================================================================
# Data types - Helpers
# =============================================================================


@dataclass
class Counts:
    """Tally of outcomes results ("added", "deleted", etc.), for
    the end-of-run summary.

    (Future self: Counts seems weird, but don't nuke it. It's useful. читай.)

    This is essentially a general aggregator of outcome results that
    tallies up results for you, instead of making callers do it.

    The main attribute is counters. A dict of counters keyed by outcome
    summary name (e.g. "added", "deleted", "matched", etc. -- .summary
    attributes from ResultOutcome enums).

    Outcomes are added via bottom level add_result. Each time an outcome
    is added there, the counter for that outcome's type (e.g. "added")
    is incremented by 1. If that key doesn't exist yet in the dict it is
    added. Multiple Counts can also be merged together.

    Counts does not care what the results are or where they came from.
    You can add results for all EntryResult objects generated from
    a single JSON file to get its added/failed/skipped, etc. counters;
    you could aggregate all EntryResults from an entire run; you can add
    a random mismatch if you want. All it does is aggregate counters.

    Example after counting 3 added, 1 skipped, and 1 no_change result:

        {
            "added": 3,
            "no_change": 1,
            "skipped": 1,
        }
    """

    counters: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    warned: int = 0
    succeeded: int = 0

    @property
    def total(self) -> int:
        """Total number of results counted, across all outcomes."""
        return sum(self.counters.values())

    def add_file_results(self, results: list[FileResult]) -> None:
        """Batch increment the counter for all results in multiple files."""
        for r in results:
            self.add_file_result(r)

    def add_file_result(self, r: FileResult) -> None:
        """Batch increment the counter for all results in a file."""
        self.add_results(r.entries)

    def add_results(self, results: list[EntryResult]) -> None:
        """Batch increment the counter for multiple results."""
        for r in results:
            self.add_result(r)

    def add_result(self, r: EntryResult) -> None:
        """Increment the counter for a result's outcome."""

        # general error/warn/success counts (separate from named counters -
        # provides way to tally total error/warn/success encountered, as they can
        # be split among different result types e.g. "error", "mismatched" are
        # both error types.)
        if r.error:
            self.failed += 1
        elif r.warning:
            self.warned += 1
        else:
            self.succeeded += 1

        # update named counters that specify the operation type e.g. "added",
        # "removed". NOTE: you can have clear error types in here also (e.g.
        # "error"). That's ok. These named counters are tallied in summary
        # independent of the attributes that are updated above.

        # r.outcome.summary -> the 'summary' attr on an ResultOutcome object
        # e.g. "added", "deleted", "matched", etc.
        counter_name = r.outcome.summary
        self.counters[counter_name] = self.counters.get(counter_name, 0) + 1

    def add_counts(self, other: "Counts") -> None:
        """Add another Counts into this one."""
        # Go through each counter this class declares, and add other's
        # value for that same counter into ours.
        for counter_name, count in other.counters.items():
            self.counters[counter_name] = self.counters.get(counter_name, 0) + count
        self.failed += other.failed
        self.warned += other.warned
        self.succeeded += other.succeeded

    def summary(self) -> str:
        """Return a one-line summary of the nonzero counts, alphabetical."""
        parts = []
        for name, count in sorted(self.counters.items()):
            if count:
                parts.append(f"{count} {name.replace('_', ' ').replace('-', ' ')}")
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


def _handle_json(path: Path, recursive: bool, logger: Logger) -> list[Result]:
    """Discover and load every JSON file under a path.

    Args:
        path (Path): A JSON file or a directory.
        recursive (bool): Descend into subdirectories when True.

    Returns:
        list[JsonFile]: One loaded file per discovered path, in sorted
            order. A path that produced no files yields an empty list.
    """

    results = []

    # find all .json files in the path (returns Path objects)
    logger.info(f"\n─── Scan JSON files at {path}... ───")
    files = _discover_json(path, recursive=recursive)
    if not files:
        results.append(
            Result(
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"no JSON files at {path}",
            )
        )

    # validate each discovered file and parse into JsonFile object
    # (_parse_json validates JSON schema, parses entries
    # and returns everyhing as a JsonFile object)
    parsed_files = [_parse_json(p) for p in files]

    # Create a JsonValidationResult object for each JsonFile,
    # indicating success or not
    logger.info(f"\n─── Validate and parse JSON ───")
    for parsed_file in parsed_files:
        outcome = ResultOutcome.JSON_PASSED
        had_error = False
        message = None
        # .errors is a list of strings, one for each error encountered
        if parsed_file.errors:
            outcome = ResultOutcome.JSON_MALFORMED
            # If JsonEntry.errors exists and is populated, then it is
            # a MalformedEntry object - an object for parsed json data
            # that could not be successfully parsed into a NounEntry.
            # .errors contains a list of strings, grouped as follows:
            # - a string for each file-level error
            # - a string PER failed entry, with all those errors bulleted
            message = "\n" + "\n".join(parsed_file.errors)
            had_error = True

        result = JsonValidationResult(
            path=parsed_file.path,
            outcome=outcome,
            # attach the JsonFile object
            # which contains successfully parsed entries,
            # errors, etc.
            file=parsed_file,
            error=had_error,
            message=message,
        )
        result.print_result_summary(logger)
        results.append(result)
    return results


def _parse_json(path: Path) -> JsonFile:
    """Read, validate, and parse one JSON file. Never raises.

    Checks the file's top-level keys and each entry. Every problem found
    is recorded, and parsing continues past individual failures so a
    file with several bad entries reports all of them.

    Args:
        path (Path): Path to the JSON file.

    Returns:
        JsonFile: The loaded file. errors is empty when the file is
            clean; otherwise it holds one formatted message per problem.
    """
    json_file = JsonFile(path=path)
    errors = []

    try:
        text = path.read_text(encoding="utf-8")
        doc = json.loads(text)
        if not isinstance(doc, dict):
            errors.append(f"top level must be an object, got {type(doc).__name__}")

        # Validate top-level shape of JSON file
        errors.extend(
            check_fields(
                doc,
                required=REQUIRED_FILE_FIELDS,
                no_extra=True,
            )
        )

        # get categry and entries entry in the JSON file
        category = doc.get("category", "unknown")
        entries = doc.get("entries")

        # parse individual entries:
        # parse_entries returns list of JsonEntry objects which can
        # be either NounEntry or MalformedEntry (for malformed case)
        parsed = parse_entries(entries, category)

        successful = []
        for entry in parsed:
            if entry.malformed:
                # separate the errors per entry
                # so that a batch of errors can
                # be printed for each malformed entry
                errors.append(entry.summary)
            else:
                successful.append(entry)
        json_file.entries = successful
        json_file.category = category
    except OSError as exc:
        errors.append(f"cannot read file: {exc}")
    except json.JSONDecodeError as exc:
        errors.append(
            f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        )

    # format any discovered erros and set into JsonFile to return.
    json_file.errors = errors

    return json_file


def parse_entries(raw_entries: list[dict], category: str) -> list[JsonEntry]:
    """
    Takes raw entries at "entries" key in JSON and parses
    them into validated NounEntry objects (which are what
    get fed to _op_* functions)

    Parses every entry and collects the failures rather than stopping
    at the first one, so a file with several bad entries reports all of
    them in one pass.

    Args:
        raw_entries (list[dict]): The raw entry dicts from a
            JsonEntries object.
        category (str): The file-level category, applied to each entry.

    Returns:
        list[JsonEntry]: Eache element is either NounEntry (The entries
            that parsed successfully) or MalformedEntry (the ones that
            failed). A file can produce both. Both classes inherit
            from JsonEntry class.
    """
    return [parse_entry(raw, category) for raw in raw_entries]


def parse_entry(raw: dict, category: str) -> JsonEntry:
    """Parse and validate one JSON entry into a NounEntry or MalformedEntry.

    The shape is inferred from the blocks present: indeclinable means no
    blocks, only singular means singular-only, only plural means
    plural-only, both means a normal noun.

    Collects every problem found rather than stopping at the first, so
    an entry with several bad fields reports all of them. Returns a
    NounEntry when every check passes, or a MalformedEntry carrying the
    errors when any fail.

    Args:
        raw (dict): The raw entry dict from the file's "entries" list.
        category (str): The file-level category, applied to this entry.
        path (Path): Path to the source file. Stored on the returned
            entry (both variants carry it) and used in error messages.

    Returns:
        JsonEntry: A NounEntry if the entry is valid, otherwise a
        MalformedEntry with one message per problem found.
    """

    errors = []

    if not isinstance(raw, dict):
        return MalformedEntry(errors=["entry must be an object"])

    def check_indeclinable():
        indeclinable = raw.get("indeclinable")
        # Indeclinable nouns have no declension blocks.
        if indeclinable and ("singular" in raw or "plural" in raw):
            return indeclinable, [
                "indeclinable entries must not have singular or plural"
            ]
        return indeclinable, []

    def check_singular():
        singular = raw.get("singular")
        if not singular:
            return singular, []
        singular, decl_errors = _parse_declension(singular)
        return singular, [f"(Singular block): {e}" for e in decl_errors]

    def check_plural():
        plural = raw.get("plural")
        if not plural:
            return plural, []
        errs = []
        forms = [plural] if isinstance(plural, dict) else plural
        if not forms:
            errs.append("plural must be a non-empty list")
        else:
            # there can be multiple plural forms for a single noun
            plural_forms = []
            for i, form in enumerate(forms, start=1):
                decl, decl_errors = _parse_declension(form)
                prefix = "(Plural block"
                if len(forms) > 1:
                    prefix += f"{prefix} {i}"
                prefix += "): "
                errs.extend(f"{prefix}{e}" for e in decl_errors)
                if decl is not None:
                    plural_forms.append(decl)
            plural = tuple(plural_forms) if plural_forms else None
        return plural, errs

    def check_word(word, singular, plural):
        # word must equal the nominative of the first block.
        # Only checkable when the blocks parsed and at least one exists.
        expected = None
        if singular:
            expected = singular.nominative
        elif plural:
            expected = plural[0].nominative
        if expected and word != expected:
            return [f"word {word!r} does not match the nominative form {expected!r}"]
        return []

    # Validate required and optional keys, checked through the shared helper.
    errors.extend(
        check_fields(
            raw,
            required=REQUIRED_ENTRY_FIELDS,
            optional=OPTIONAL_ENTRY_FIELDS,
            no_extra=False,  # dont fail if extra keys encountered
        )
    )

    # Get basic data from the entry

    word = raw.get("word")  # dictionary form of word
    gender_str = raw.get("gender")
    animate = raw.get("animate")  # JSON uses True/False, not 0/1 like db

    # indeclinable, optional boolean.
    indeclinable, errs = check_indeclinable()
    errors.extend(errs)

    # Parse singular and plural blocks.
    singular, errs = check_singular()
    errors.extend(errs)
    plural, errs = check_plural()
    errors.extend(errs)
    if not singular and not plural and not indeclinable:
        errors.append("entry must have singular or plural, or be indeclinable")

    # ensure word is in dictionary form
    errors.extend(check_word(word, singular, plural))

    if errors:
        # prefix word data if found, to make error messages helpful
        entry_word = word if word else "Unknown (could not be parsed)"
        return MalformedEntry(word=entry_word, errors=errors)

    return NounEntry(
        word=word,
        gender=Gender(gender_str),
        animate=animate,
        category=category,
        indeclinable=indeclinable,
        singular=singular,
        plural=plural,
    )


def _parse_declension(block: dict) -> tuple[Declension | None, list[str]]:
    """Parse one declension block, collecting errors instead of raising.

    Every key must be a known case name; every value a non-empty string;
    the six standard cases must all be present. Returns the Declension
    when it can be built, or None with a list of messages when it
    cannot.

    Args:
        block: The dict of case keys to surface forms.

    Returns:
        tuple[Declension | None, list[str]]: The declension, or None if
            it could not be built, plus one message per problem found.
    """
    if not isinstance(block, dict):
        return None, ["declension block must be an object"]

    # Validate the block through the shared helper: the six standard
    # cases are required, the four rare cases are optional, and any key
    # that is not a case name is rejected.
    errors = check_fields(
        block,
        required=REQUIRED_DECLENSION_FIELDS,
        optional=OPTIONAL_DECLENSION_FIELDS,
        no_extra=True,  # fail if extra keys are found
    )
    if errors:
        return None, errors

    # Build the Declension from the validated block. The keys have been
    # checked to be known case names, so JSON_TO_CASE[key] is safe.
    # .name.lower() turns the Case member into its Declension field name
    # (NOMINATIVE -> "nominative").
    kwargs = {JSON_TO_CASE[key].name.lower(): value for key, value in block.items()}
    return Declension(**kwargs), []


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
                result.print_result_summary(logger)
                continue

            # Step 4: insert.
            insert_tree(conn, table, entry)
            conn.commit()
            result = EntryResult(word=entry.word, outcome=ResultOutcome.ADDED)
            results.append(result)
            result.print_result_summary(logger)
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            result.print_result_summary(logger)
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            result.print_result_summary(logger)

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
                result.print_result_summary(logger)
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
                result.print_result_summary(logger)
                continue

            # If every match already has this content, there is nothing to change.
            # This covers N=1 and also the case where several homonyms all match, so
            # a --force-update does not needlessly delete and reinsert identical data.
            if roots and all(
                normalize_tree(load_tree(conn, table, r.code)) == entry for r in roots
            ):
                result = EntryResult(word=entry.word, outcome=ResultOutcome.NO_CHANGE)
                results.append(result)
                result.print_result_summary(logger)
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
            result.print_result_summary(logger)
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            result.print_result_summary(logger)
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            result.print_result_summary(logger)

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
                result.print_result_summary(logger)
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
                result.print_result_summary(logger)
                continue

            # Step 4: delete every match.
            for r in matches:
                delete_tree(conn, table, r.code)
            conn.commit()
            result = EntryResult(word=entry.word, outcome=ResultOutcome.DELETED)
            results.append(result)
            result.print_result_summary(logger)
        except RumorphError as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            result.print_result_summary(logger)
        except Exception as exc:
            conn.rollback()
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            result.print_result_summary(logger)

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
                result.print_result_summary(logger)
                continue

            # Step 3: compare each root's content against the entry.
            matched = any(
                normalize_tree(load_tree(conn, table, r.code)) == entry for r in roots
            )
            if matched:
                # outcome depends on if there were multiple matches or only one.
                # If multiple, can not explicitly verify.
                multiple = len(roots) > 1
                message = None
                if multiple:
                    # multiple exactly matches - ambiguous state.
                    outcome = ResultOutcome.MATCHED_AMBIGUOUS
                    message = f"{len(roots)} roots for this word"
                else:
                    # a single match
                    outcome = ResultOutcome.MATCHED

                result = EntryResult(
                    word=entry.word, outcome=outcome, warning=multiple, message=message
                )
                results.append(result)
                result.print_result_summary(logger)
            else:
                result = EntryResult(
                    word=entry.word,
                    outcome=ResultOutcome.MISMATCHED,
                    error=True,
                    message="database content differs",
                )
                results.append(result)
                result.print_result_summary(logger)
        except RumorphError as exc:
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=str(exc),
            )
            results.append(result)
            result.print_result_summary(logger)
        except Exception as exc:
            result = EntryResult(
                word=entry.word,
                outcome=ResultOutcome.ERROR,
                error=True,
                message=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            result.print_result_summary(logger)

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
        line = f"  {r.outcome.symbol} {r.check}"
        if r.message:
            line += f"\n      {r.message}"
        if r.error:
            logger.error(line)
        else:
            logger.info(line, code=r.outcome.code)
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


# Delimiters for the four summary levels. Run is heaviest, file is
# lightest, so nesting is visible at a glance.
_RUN_RULE = "═" * 60
_OP_RULE = "─" * 60
_FILE_RULE = "─" * 40


def build_entry_lines(results: list[EntryResult], indent: str = "    ") -> str:
    """Render one line per entry result.

    Args:
        results: The EntryResult objects for one file.
        indent: Leading whitespace for every line.

    Returns:
        One line per result, newline-joined. Empty string if no results.
    """
    lines = []
    for r in results:
        lines.append(f"{indent}{r.summary_line}")
    return "\n".join(lines)


def build_file_summary(
    operation: str,
    file_result: FileResult,
    index: int,
    build_full: bool,
) -> str:
    """Render the summary block for one file.

    Args:
        operation (str): The operation name.
        file_result (FileResult): The FileResult for this file. Provides
            the path, category, entries, and the counts derived from
            them.
        index (int): 1-based index of the file within the run.
        build_full (bool): If True, include the individual entry result
            lines below the file header.

    Returns:
        str: The file summary.
    """
    counts = Counts()
    counts.add_file_result(file_result)

    lines = [
        _FILE_RULE,
        f" FILE #{index}",
        _FILE_RULE,
        f"   Path:      {file_result.file}",
        f"   Operation: {operation}",
        f"   Category:  {file_result.category}",
    ]
    if file_result.error:
        lines.append(f"   Error:     {file_result.message}")
    elif file_result.message:
        # non-error message for user such as json validate
        # indicating that no files were actually processed.
        # (.message is a catch all)
        lines.append(f"   Message:   {file_result.message}")
    else:
        lines.append(f"   Result:    {counts.summary()}")
    lines.append(_FILE_RULE)
    if build_full:
        entries = build_entry_lines(file_result.entries)
        if entries:
            lines.append(entries)
    return "\n".join(lines)


def build_operation_summary(
    operation: str, file_results: list[FileResult], build_full: bool
) -> str:
    """Render the summary block for one operation.

    Computes the operation's totals from the file results, lists the
    files processed, and includes a file summary for each file.

    Args:
        operation (str): The operation name.
        file_results (list[FileResult]): The FileResult objects for this
            operation.
        build_full (bool): If True, each file summary includes its
            individual entry result lines.

    Returns:
        The operation summary as a string.
    """

    # Get summary counts for entire operation
    totals = Counts()
    totals.add_file_results(file_results)

    lines = [
        _OP_RULE,
        f" OPERATION: {operation}",
        _OP_RULE,
        "   Files:",
    ]
    for r in file_results:
        p = r.file
        lines.append(f"     - {p}")
    lines.append(f"   Result: {totals.summary()}")
    lines.append(_OP_RULE)

    for i, r in enumerate(file_results, start=1):
        summary = build_file_summary(operation, r, i, build_full)
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


def build_run_summary(
    operation: str,
    file_results: list[FileResult],
    build_full: bool,
) -> str:
    """Render the top-level run summary, including all nested sections.

    Computes the run totals from the file results and includes an
    operation summary, which in turn includes a file summary per file.

    Args:
        operation (str): The operation name.
        file_results (list[FileResult]): The FileResult objects for the
            run.
        build_full (bool): If True, each file summary includes its
            individual entry result lines.

    Returns:
        str: The run summary.
    """

    # operation summary string
    operation_summary = build_operation_summary(operation, file_results, build_full)

    # Get summary counts for entire run
    totals = Counts()
    totals.add_file_results(file_results)

    lines = [
        _RUN_RULE,
        " RUN SUMMARY",
        _RUN_RULE,
        f"   Operation:       {operation}",
        f"   Files processed: {len(file_results)}",
        f"   Entries:         {totals.total}",
        f"   Result:          {totals.summary()}",
        _RUN_RULE,
        "",
        operation_summary,
        f"TOTAL: {totals.summary()}",
    ]
    return "\n".join(lines)


def build_problems_block(title: str, problems: list, prefix: str = "Issue") -> str:
    if not problems:
        return ""
    rule = "═" * 60
    inner_rule = "─" * 60
    lines = [rule, f" {title}", rule]
    for i, r in enumerate(problems, start=1):
        lines.append(
            f"{inner_rule}\n{prefix} #{i}:\n{r.label}:\n{r.message or r.outcome.summary}\n{inner_rule}"
        )
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
        logger.error(build_problems_block("ERRORS", errors, prefix="Error"))
    if warnings:
        logger.warning(build_problems_block("WARNINGS", warnings, prefix="Warning"))


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
    common.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories.",
    )

    parser = argparse.ArgumentParser(prog="rumorph", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    # JSON only operations (no db connection)
    json = sub.add_parser("json", parents=[common], help="Operations on JSON files.")
    json.add_argument("path", type=Path, help="JSON file or directory.")
    json.add_argument(
        "operation",
        choices=[
            "validate",
        ],
    )

    # Operations directly on the database
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
        ],
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
    ap.add_argument(
        "--full-summary",
        action="store_true",
        help="Post-run summary re-prints entry results (useful for final CI logs).",
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

    # load the config file which has database configuration
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error(f"{exc}")
        return 1

    if args.command == "util":
        results = _handle_util_command(args, config, logger)
    elif args.command == "entries" or args.command == "json":
        results = _handle_json_dependent_commands(args, config, logger)
    else:
        raise RumorphError(f"unknown subcommand: {args.command!r}")

    errors = [r for r in results if r.error]
    warnings = [r for r in results if r.warning]

    print_problems(errors, warnings, logger)

    if errors:
        return 1
    # don't return 1 on warnings because
    # they are not true errors and don't
    # want to crash CI
    return 0


def _handle_util_command(args, config: dict, logger: Logger) -> list[Result]:
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

    # open the database connection
    conn = connect(config)

    results = []
    if args.sanity:
        results.extend(check_transformations(conn, config["table"], logger))
    if args.word:
        # no results to return
        word_search(conn, config["table"], args.word, args.case_insensitive, logger)
    return results


def _handle_json_dependent_commands(args, config: dict, logger: Logger) -> list[Result]:
    """Shared entrypoint for both "json" and "entries" subcmmands,
    as they share an identical opening: discovery, validation, and
    parsing of JSON files.

    After this:
    - json subpaser: return validation results and stop. No db connectin.
    - entries subparser: open db connection and run the operation over the files
      that loaded cleanly.

    Args:
        args (argparse.Namespace): Parsed arguments.
        config (dict): The [mysql] dict.
        logger (Logger): The logger.

    Returns:
        list[Result]: Load errors, FileResults, and TransformResults.
    """

    # Step: discover, validate, and parse all JSON files
    # ** common to the 'json' and 'entries' subparsers **
    # Note: _handle_json returns a list of JsonValidationResult object
    validation_results = _handle_json(
        args.path, recursive=not args.no_recursive, logger=logger
    )

    # Step: branch based on subparser.

    # Step: If the "entries" subparser was enabled,
    # run database operation (add, delete, etc.) against error-free JSON
    if args.command == "entries":
        # filter JsonValidationResults based on success state
        validated_results = []
        malformed_results = []
        for result in validation_results:
            if result.error:
                malformed_results.append(result)
            else:
                validated_results.append(result)

        # get JsonFile objects from the validated results
        validated_files = [r.file for r in validated_results]

        # call operation subrunner, sending only JsonFile objects that passed validation
        # (do the filtering here rather than in _handle_entries_command because else it will
        # keep skipping the same malformed files during each database operation)
        results = _handle_entries_command(args, config, validated_files, logger)

        # add JsonValidationResult objects from the failed files so it has all
        results.extend(malformed_results)

        return results
    elif args.command == "json":
        # only JSON validation
        return validation_results
    else:
        raise ValueError(f"Unknown subparser: {args.command}")


def _handle_entries_command(
    args, config: dict, parsed_json_files: list[JsonFiles], logger: Logger
) -> list[Result]:
    """Handle entries subcommand: run one operation over one or more JSON files.

    Returns:
        list[Result]: The FileResult objects for each file processed,
            plus the TransformResult objects if verify-all ran. Both
            subclass Result; callers filter on .error and .warning.
    """

    # open the database connection
    conn = connect(config)

    all_results = []

    # operation supplied to entries subparser ("add", "delete", etc.)
    operation = args.operation

    # Step 2: run the operation over every file, collecting what the
    # end-of-run summary needs: the results per file, and the totals.
    file_results = []
    for parsed_json_file in parsed_json_files:
        path = parsed_json_file.path
        logger.info(f"\n─── {path} ───")

        try:
            entries = parsed_json_file.entries
            category = parsed_json_file.category

            # Apply entries from the parsed JSON file against the current
            # operation (e.g. add all entries)
            entry_results = _dispatch_operation(
                operation=operation,
                case_insensitive=args.case_insensitive,
                force_update=args.force_update,
                force_upstream=args.force_upstream,
                force_delete=args.force_delete,
                config=config,
                conn=conn,
                entries=entries,
                logger=logger,
            )

            # create FileResult for end of run summary
            file_result = FileResult(
                outcome=ResultOutcome.COMPLETE,
                file=path,
                category=category,
                entries=entry_results,
            )
            # creating FileResult automatically creates a Counts
            # on it which includes count summaries for each entry.
            # print summary live (re-printed at end-of-run summary)
            logger.info(f"\n  {file_result.counts.summary()}")
            file_results.append(file_result)
        except RumorphError as exc:
            # log error and continue to next file
            logger.error(f"  ! {path}: {exc}")
            file_results.append(
                FileResult(
                    outcome=ResultOutcome.ERROR, file=path, error=True, message=str(exc)
                )
            )

    # Step: If verify-all supplied, runs final sanity/transformation check
    if operation == "verify-all":
        # returns TransformResult
        all_results.extend(check_transformations(conn, config["table"], logger))

    # Step: build the run summary from what was collected, and print it
    # once.
    logger.info(
        build_run_summary(
            operation=operation,
            file_results=file_results,
            build_full=args.full_summary,
        )
    )

    all_results.extend(file_results)
    return all_results


def _dispatch_operation(
    operation: str,
    case_insensitive: bool,
    force_update: bool,
    force_upstream: bool,
    force_delete: bool,
    config: dict,
    conn: Connection,
    entries: list[NounEntry],
    logger: Logger,
) -> list[EntryResult]:
    """Run one operation against a set of entries.

    Takes the operation and the flags it needs as explicit parameters
    rather than reading them off the parsed arguments, so the caller can
    invoke it once per operation when a run supplies more than one.

    Args:
        operation (str): name of operation supplied to 'entries' subparser
            (e.g. "add", "delete", verify", "verify-all")
        case_insensitive (bool): Whether word matching ignores case.
        force_update (bool): Allow replacing all matches when update is
            ambiguous.
        force_upstream (bool): Allow replacing upstream entries.
        force_delete (bool): Allow deleting all matches when delete is
            ambiguous.
        config (dict): The [mysql] dict.
        conn (Connection): The database connection.
        entries (list[NounEntry]): The validated NounEntry objects to
            operate on.
        logger (Logger): The logger.

    Returns:
        list[EntryResult]: One EntryResult per entry, in input order.

    Raises:
        ValueError: If operation is not a known entries operation.
    """
    # Run the operation
    table = config["table"]
    if operation == "add":
        return op_add(conn, table, entries, logger, case_insensitive)
    elif operation == "update":
        return op_update(
            conn, table, entries, logger, force_update, force_upstream, case_insensitive
        )
    elif operation == "delete":
        return op_delete(conn, table, entries, logger, force_delete, case_insensitive)
    elif operation in ["verify", "verify-all"]:
        return op_verify(conn, table, entries, logger, case_insensitive)
    else:
        raise ValueError(f'Unknown entries operation "{operation}"')


def _discover_json(path: Path, recursive: bool) -> list[Path]:
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
