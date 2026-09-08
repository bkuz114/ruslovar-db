# manager.py

Command-line tool for managing the `runouns` MySQL database of Russian noun morphology. Reads JSON files containing noun entries (proper names, patronymics, surnames), validates them, and inserts their declension forms into `nouns_morf` with correct parent-child relationships. Also supports deleting entries, checking the database against JSON, looking up words, and running database sanity checks.

## Background

`runouns` started as a MySQL database containing the Sshra dump — a large, pre-existing dataset of Russian noun morphology. The dump covers general vocabulary: common nouns with their full declension tables (all case forms, singular and plural, with grammatical metadata).

The Sshra dump was imported into the database as the foundation. But it has gaps — it does not include many proper names, patronymics, surnames, and other entries that users may search for.

To fill those gaps, custom entries were added on top of the Sshra data. These are maintained through this tool.

Alongside the custom entries, the database has also received:

- **Indexes** on lookup-critical columns (`code`, `code_parent`, `word`) to improve query performance.
- **Data fixes** for known gaps in the Sshra dump (e.g., linking "дети" as the suppletive plural of both "дитя" and "ребенок").
- **Custom columns** (`is_custom`, `created_at`, `category`) to track which entries were added by this tool and when.

This tool is used as a general purpose manager to make further modifications to the growing modifications to `nouns_morf`. It's main feature is the ability to take `JSON` files with nouns and their declined forms, and add them to the table. It also supports updating, deleting, or verifying based on such files.

In addition, it provides basic utility functions (e.g. database sanity, and single noun lookup) as convenience and build functions.

This README walks through the usage of the tool.

## Requirements

- Python 3.8+
- `pymysql` (tested with 1.2.0)

Install dependencies directly:

```bash
pip install pymysql==1.2.0
```

Or install from the requirements file:

```bash
pip install -r requirements.txt
```

Note: The requirements file pins `pymysql==1.2.0`, the version this tool has been tested against. Other versions may work but are untested.

## Configuration

The tool reads MySQL connection settings from an INI file. It searches the following locations in order and uses the first one found:

1. `./.mysql_import.conf`
2. `~/.mysql_import.conf`
3. `/etc/.mysql_import.conf`

You can also specify an explicit path with `--config`.

Example `.mysql_import.conf`:

```ini
[mysql]
host = localhost
port = 3306
user = your_username
password = your_password
database = runouns
charset = utf8mb4
```

`port` and `charset` are optional (defaults: `3306` and `utf8mb4`).

## Usage

The tool has two command groups: `file` for file-based operations and `util` for standalone operations.

### File operations

```bash
# Add entries from JSON (skips entries that already exist)
python manager.py file add path/to/file.json
python manager.py file add path/to/directory/

# Replace existing entries with fresh data
python manager.py file update path/to/file.json

# Delete entries listed in JSON (prompts for confirmation)
python manager.py file delete path/to/file.json

# Validate JSON structure without touching the database
python manager.py file validate-json path/to/file.json

# Check DB entries against a JSON file
python manager.py file verify path/to/file.json

# Check DB entries against JSON plus run full DB sanity checks
python manager.py file verify-all path/to/file.json
```

### Utility operations

```bash
# Display an entry by word
python manager.py util --word Иван

# Run DB sanity checks (indexes, columns, data fixes)
python manager.py util --sanity
```

### Common flags

Available on both `file` and `util`:

```
--config PATH     Path to MySQL config file
--color MODE      Color output: auto (default), always, or never
--stderr          Route all output to stderr
```

### File operation flags

```
--no-recursive    Do not process subdirectories when path is a directory
--no-dump         Do not print full rows after add/update
```

## JSON input format

```json
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
```

### Indeclinable entries

Indeclinable nouns (no declension forms) use the `indeclinable` flag:

```json
{
  "category": "surname",
  "entries": [
    {
      "word": "Бондаренко",
      "gender": "общ",
      "animacy": 1,
      "indeclinable": true
    }
  ]
}
```

## Operations

| Operation | Description |
|-----------|-------------|
| `add` | Insert entries, skipping those that already exist |
| `update` | Delete and re-insert existing entries, insert new ones |
| `delete` | Remove entries after explicit confirmation |
| `validate-json` | Validate JSON structure without DB access |
| `verify` | Check DB entries against a JSON file |
| `verify-all` | Check DB entries plus run full DB sanity checks |
| `--word` | Display an entry by word |
| `--sanity` | Run DB sanity checks |

## Sanity checks

The `--sanity` operation verifies:

- Expected indexes exist: `code_idx`, `code_parent_idx`, `word_idx`
- Data fixes are applied: `дети` has parent links to both `дитя` and `ребенок`
- Custom columns exist: `is_custom`, `created_at`, `category`

Each check logs PASS or FAIL individually. Exit code is `1` if any check fails, making it suitable for CI pipelines.

## Exit codes

- `0` — success
- `1` — any error occurred (validation error, DB error, sanity check failure, verification mismatch)

## Notes

- Import operations use case-sensitive matching (`Петр` and `Пётр` are distinct entries).
- The `--word` lookup is case-insensitive (`иван` matches `Иван`) while retaining ё/е distinction.
- One transaction per file: all entries in a file commit or roll back together.
- All rows for a file share a single `created_at` timestamp.
