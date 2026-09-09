#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# build-db.sh
#
# Regenerates db/nouns_morf.sql.gz from the upstream Russian morphology dump
# by applying the project's SQL and Python transformations.
#
# Usage:
#   ./scripts/build-db.sh
#
# Configuration is read from environment variables (see below). Defaults are
# provided for local development; override them in CI or via a .env file if
# needed.
#
# Prerequisites:
#   - MySQL running and reachable at DB_HOST:DB_PORT
#   - mysql and mysqldump clients installed
#   - python3 installed with the transformation and verification dependencies
# =============================================================================

# =============================================================================
# CONFIGURATION
# =============================================================================
# All configurable values live here. Override via environment variables.
# =============================================================================

# -----------------------------------------------------------------------------
# Paths & Anchors
# -----------------------------------------------------------------------------

# Absolute path to this script's directory (works regardless of CWD)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Repository root (one level up from SCRIPT_DIR, assuming script lives in bin/)
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# -----------------------------------------------------------------------------
# Build Environment
# -----------------------------------------------------------------------------

# Temporary working directory (created fresh on each run, cleaned up on exit)
BUILD_DIR="$(mktemp -d)"

# Generated config file consumed by manager.py
CONFIG_FILE="$BUILD_DIR/.mysql_import.conf"

# -----------------------------------------------------------------------------
# Database Connection
# -----------------------------------------------------------------------------

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-root}"
DB_PASSWORD="${DB_PASSWORD:-password}"

# Name of the temporary build database used during the build process.
#
# This database is created and used by the build script ONLY for making
# the final sql dump.
#
# The database name below does NOT appear in the final dump: the export
# step (export_database) intentionally omits --databases and
# --add-drop-database (which would otherwise bake it in to the dump)
# so that users can import the dump into whatever database name they chose.
# This is vital for this project, as the fullstack, containerized application
# relies on the database (which will run in a Docker container) being called
# runouns (it's hardcoded as an env var in compose.yml, in ruslovar-api,
# for the FastAPI server to consume)
DB_NAME="${DB_NAME:-runouns_build}"

# -----------------------------------------------------------------------------
# Source & Output
# -----------------------------------------------------------------------------

# Upstream Russian morphology dump (original, untransformed)
SOURCE_URL="${SOURCE_URL:-https://raw.githubusercontent.com/sshra/database-russian-morphology/master/words-russian-nouns-morf.sql.gz}"

# Final gzipped dump consumed by the Dockerfile
OUTPUT_FILE="${OUTPUT_FILE:-$REPO_ROOT/db/nouns_morf.sql.gz}"

# -----------------------------------------------------------------------------
# SQL Transformation
# -----------------------------------------------------------------------------

# Applies indexes, data fixes, and other structural changes before the
# Python transformation runs.
SQL_TRANSFORM="${SQL_TRANSFORM:-$REPO_ROOT/sql/transformations.sql}"

# -----------------------------------------------------------------------------
# Python Transformation & Verification (manager.py)
# -----------------------------------------------------------------------------

# Main script for adding JSON entries to the database and verifying the result
MANAGER="${MANAGER:-$REPO_ROOT/tools/manager.py}"

# Directory containing JSON entry files consumed by manager.py
JSON_ENTRIES="${JSON_ENTRIES:-$REPO_ROOT/custom-entries}"

# Shared base invocation (subcommand + JSON directory, reused by both calls)
#
# Note: Commands are stored as Bash arrays. Each array element is quoted, so
# paths with spaces or special characters stay intact as single arguments.
# When the command runs, "${ARRAY[@]}" expands to each element as its
# own separate argument—nothing gets split or merged.
# A plain string can't do this: quoted, it becomes one argument; unquoted,
# it splits on whitespace. and paths get mangled.
MANAGER_BASE_INVOCATION=(
    "$MANAGER"
    file
    "$JSON_ENTRIES"
)

# Full commands for transformation and verification steps
PYTHON_TRANSFORM=(
    "${MANAGER_BASE_INVOCATION[@]}"
    add
    --config "$CONFIG_FILE"
    --no-dump
)

PYTHON_VERIFY=(
    "${MANAGER_BASE_INVOCATION[@]}"
    verify-all
    --config "$CONFIG_FILE"
)

# -----------------------------------------------------------------------------
# Dedicated cleanup
# -----------------------------------------------------------------------------

cleanup() {
    local exit_code=$?
    echo ""
    echo "================================================================================"
    echo "=== Cleanup ==="
    echo "================================================================================"

    # Drop the build database if it exists
    if mysql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --user="$DB_USER" \
        --password="$DB_PASSWORD" \
        --execute="DROP DATABASE IF EXISTS \`$DB_NAME\`;" >/dev/null 2>&1; then
        echo "Dropped database: $DB_NAME"
    else
        echo "Warning: could not drop database $DB_NAME (it may not exist or MySQL is unreachable)"
    fi

    # Remove temp directory
    rm -rf "$BUILD_DIR"
    echo "Removed build directory: $BUILD_DIR"

    exit "$exit_code"
}
trap cleanup EXIT

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------

mysql_exec() {
    mysql \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --user="$DB_USER" \
        --password="$DB_PASSWORD" \
        "$@"
}

mysql_exec_db() {
    mysql_exec --database="$DB_NAME" "$@"
}

# -----------------------------------------------------------------------------
# Step banner helper
# -----------------------------------------------------------------------------

step_banner() {
    echo ""
    echo "================================================================================"
    echo "=== $1 ==="
    echo "================================================================================"
}

# -----------------------------------------------------------------------------
# 1. Check prerequisites
# -----------------------------------------------------------------------------

check_prerequisites() {
    step_banner "Checking prerequisites"

    local missing=()

    command -v mysql >/dev/null 2>&1 || missing+=("mysql client")
    command -v mysqldump >/dev/null 2>&1 || missing+=("mysqldump")
    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v gzip >/dev/null 2>&1 || missing+=("gzip")

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "ERROR: Missing required tools: ${missing[*]}" >&2
        exit 1
    fi

    echo "All required tools present."
}

# -----------------------------------------------------------------------------
# 2. Fetch upstream dump
# -----------------------------------------------------------------------------

fetch_source() {
    step_banner "Fetching upstream dump"
    echo "Source URL: $SOURCE_URL"
    echo "Destination: $BUILD_DIR/original.sql.gz"
    echo ""

    curl -L --fail --output "$BUILD_DIR/original.sql.gz" "$SOURCE_URL"

    echo ""
    echo "Download complete."
}

# -----------------------------------------------------------------------------
# 3. Generate .mysql_import.conf
# -----------------------------------------------------------------------------

write_import_config() {
    step_banner "Generating .mysql_import.conf"
    echo "Config file: $CONFIG_FILE"
    echo ""

    cat > "$CONFIG_FILE" <<EOF
[mysql]
host = $DB_HOST
port = $DB_PORT
user = $DB_USER
password = $DB_PASSWORD
database = $DB_NAME
charset = utf8mb4
EOF

    echo "Wrote config:"
    echo "  host:     $DB_HOST"
    echo "  port:     $DB_PORT"
    echo "  user:     $DB_USER"
    echo "  database: $DB_NAME"
}

# -----------------------------------------------------------------------------
# 4. Create build database
# -----------------------------------------------------------------------------

create_database() {
    step_banner "Creating build database"
    echo "Database: $DB_NAME"
    echo "Host:     $DB_HOST:$DB_PORT"
    echo ""

    mysql_exec --execute="DROP DATABASE IF EXISTS \`$DB_NAME\`;"
    mysql_exec --execute="CREATE DATABASE \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

    echo "Database ready."
}

# -----------------------------------------------------------------------------
# 5. Import upstream dump
# -----------------------------------------------------------------------------

import_dump() {
    step_banner "Importing upstream dump"
    echo "Source:     $BUILD_DIR/original.sql.gz"
    echo "Target DB:  $DB_NAME"
    echo ""

    gzip --decompress --stdout "$BUILD_DIR/original.sql.gz" \
        | mysql_exec_db

    echo ""
    echo "Import complete."
}

# -----------------------------------------------------------------------------
# 6. Run SQL transformations
# -----------------------------------------------------------------------------

run_sql_transformations() {
    step_banner "Running SQL transformations"
    echo "SQL file: $SQL_TRANSFORM"
    echo "Target DB: $DB_NAME"
    echo ""

    output=$(mysql_exec_db < "$SQL_TRANSFORM" 2>&1)
    echo "$output"

    echo ""
    echo "SQL transformations applied."
}

# -----------------------------------------------------------------------------
# 7. Run Python transformation script
# -----------------------------------------------------------------------------

run_python_transformations() {
    step_banner "Running Python transformations"
    echo "Command: $PYTHON_TRANSFORM"
    echo ""

    python3 "${PYTHON_TRANSFORM[@]}"

    echo ""
    echo "Python transformations applied."
}

# -----------------------------------------------------------------------------
# 8. Run Python verification script
# -----------------------------------------------------------------------------

run_verification() {
    step_banner "Running verification"
    echo "Command: $PYTHON_VERIFY"
    echo ""

    python3 "${PYTHON_VERIFY[@]}"

    echo ""
    echo "Verification passed."
}

# -----------------------------------------------------------------------------
# 9. Export transformed database
# -----------------------------------------------------------------------------

export_database() {
    step_banner "Exporting transformed database"
    echo "Source DB:   $DB_NAME"
    echo "Output file: $OUTPUT_FILE"
    echo ""

    local output_dir
    output_dir="$(dirname "$OUTPUT_FILE")"
    mkdir -p "$output_dir"

    # WARNING: Do not add --databases or --add-drop-database to the
    # mysqldump invocation. Those flags bake the database name into
    # the dump, forcing it on anyone who imports it.
    #
    # This matters because the full-stack application expects the
    # database to be named runouns. The compose.yml in ruslovar-api
    # sets DB_NAME=runouns as an environment variable for the FastAPI
    # server. If the dump creates a database with any other name, the
    # server will look for runouns and fail to find its tables.
    #
    # Hardcoding runouns here would not solve this. Local builds use
    # a different database name (see DB_NAME above) to avoid
    # clobbering any existing runouns database on the developer's
    # machine. The dump must remain database-agnostic, like sshra's
    # original.
    mysqldump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --user="$DB_USER" \
        --password="$DB_PASSWORD" \
        "$DB_NAME" \
        --single-transaction \
        --quick \
        | gzip > "$OUTPUT_FILE"

    echo ""
    echo "Export complete."
    echo "File: $(du -h "$OUTPUT_FILE" | cut -f1)"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    step_banner "Starting database build"
    echo "Database: $DB_NAME @ $DB_HOST:$DB_PORT"
    echo "Output:   $OUTPUT_FILE"
    echo "Build dir: $BUILD_DIR"
    echo ""

    check_prerequisites
    fetch_source
    write_import_config
    create_database
    import_dump
    run_sql_transformations
    run_python_transformations
    run_verification
    export_database

    echo ""
    step_banner "Build complete"
}

main "$@"
