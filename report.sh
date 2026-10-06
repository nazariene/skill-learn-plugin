#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'report.sh: %s\n' "$*" >&2
  exit 1
}

python=python3
arguments=()
while (($#)); do
  case "$1" in
    --home|--python)
      (($# >= 2)) || fail "$1 requires a value"
      case "$1" in
        --home) arguments+=(--home "$2") ;;
        --python) python=$2 ;;
      esac
      shift 2
      ;;
    -h|--help)
      cat <<'HELP'
Usage: report.sh [--home DIRECTORY] [--python EXECUTABLE]

Generate an offline HTML report using this checkout's Python code and live settings.
Requires Python 3.11+ and PyYAML 6. Prints the generated index path.

  --home    Settings and database home (default: installed skill-learn home)
  --python  Python interpreter (default: python3)
HELP
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export PYTHONPATH="$project/plugin/server${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" -m skill_learn "${arguments[@]}" report
