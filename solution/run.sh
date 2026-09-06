#!/usr/bin/env bash
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SOLUTION_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$SOLUTION_DIR/.venv/bin/python}"
STAGE="${1:-all}"
BUILD_DIR="${2:-$SOLUTION_DIR/build}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python environment not found at $PYTHON_BIN" >&2
    echo "Run $SOLUTION_DIR/setup.sh first, or set PYTHON_BIN." >&2
    exit 1
fi

mkdir -p "$BUILD_DIR"

extract() {
    "$PYTHON_BIN" "$SOLUTION_DIR/extract_connectivity.py" \
        "$REPO_ROOT/puzzle.gds" \
        --output "$BUILD_DIR/puzzle_netlist.json" \
        --verilog-output "$BUILD_DIR/puzzle_netlist.v"
}

decode() {
    "$PYTHON_BIN" "$SOLUTION_DIR/extract_final_string.py" \
        "$BUILD_DIR/puzzle_netlist.json" \
        "${1:-$SOLUTION_DIR/known_solution_bits.txt}" \
        --expect "(* TWO STARS *)"
}

solve() {
    "$PYTHON_BIN" -u "$SOLUTION_DIR/solve_puzzle.py" \
        "$BUILD_DIR/puzzle_netlist.json" \
        --output "$BUILD_DIR/z3_solution_bits.txt"
    decode "$BUILD_DIR/z3_solution_bits.txt"
}

case "$STAGE" in
    extract)
        extract
        ;;
    decode)
        decode
        ;;
    solve)
        solve
        ;;
    all)
        extract
        decode
        ;;
    *)
        echo "Usage: $0 [all|extract|decode|solve] [build-directory]" >&2
        exit 2
        ;;
esac
