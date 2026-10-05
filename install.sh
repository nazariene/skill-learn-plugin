#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

config=${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}
destination="$config/plugins"
home=${SKILL_LEARN_HOME:-}
python=python3
while (($#)); do
  case "$1" in
    --dest|--home|--python)
      (($# >= 2)) || fail "$1 requires a value"
      case "$1" in
        --dest) destination=$2 ;;
        --home) home=$2 ;;
        --python) python=$2 ;;
      esac
      shift 2
      ;;
    -h|--help)
      cat <<'HELP'
Usage: install.sh [--dest DIRECTORY] [--home DIRECTORY] [--python EXECUTABLE]

Install the OpenCode skill-learn plugin. Requires Python 3.11+, PyYAML 6 and npm.
Keep existing settings and database files; replace all other plugin contents.

  --dest    Plugin discovery directory (default: OpenCode config/plugins)
  --home    Settings and database directory (default: <dest>/skill-learn)
  --python  Python runtime executable (default: python3)
HELP
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source="$project/plugin"
destination=$(realpath -m -- "${destination/#\~/$HOME}")
plugin=$(realpath -m -- "$destination/skill-learn")
home=${home:-$plugin}
home=$(realpath -m -- "${home/#\~/$HOME}")
[[ "$source" != "$plugin" && "$source" != "$plugin/"* && "$plugin" != "$source/"* ]] || fail "Installation must be outside the source plugin"
python=$(command -v -- "$python") || fail "Python executable not found"
[[ "$python" == /* ]] || python="$PWD/$python"
version=$("$python" --version)
[[ "$version" =~ ^Python[[:space:]]([0-9]+)\.([0-9]+) ]] || fail "Cannot determine Python version"
((BASH_REMATCH[1] > 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] >= 11))) || fail "Python 3.11+ is required"
command -v npm >/dev/null || fail "npm is required"

if [[ -f "$plugin/index.js" ]]; then
  grep -Fq 'import { setupV2 } from "./v2.mjs"' "$plugin/index.js" &&
    grep -Fq 'id: "skill-learn"' "$plugin/index.js" || fail "Unrecognized entry point: $plugin/index.js"
fi

mkdir -p -- "$destination"
staged=$(mktemp -d "$(dirname -- "$destination")/.skill-learn-install.XXXXXX")
trap 'rm -rf -- "$staged"' EXIT
cp -- "$source/"{native,child,plugin,v2}.mjs "$staged/"
mkdir -- "$staged/runtime"
cp -a -- "$source/server/skill_learn" "$source/server/pyproject.toml" "$staged/runtime/"
find "$staged/runtime" -type d -name __pycache__ -prune -exec rm -rf -- {} +
cp -- "$project/docs/settings.example.yaml" "$staged/settings.example.yaml"
cp -- "$source/"{package.json,package-lock.json} "$staged/"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$staged/runtime" "$python" -m skill_learn --help >/dev/null
npm ci --ignore-scripts --no-audit --no-fund --prefix "$staged"

mkdir -p -- "$plugin"
find "$plugin" -mindepth 1 ! -type d ! \( -name settings.yaml -o -name '*.sqlite' -o -name '*.sqlite-*' -o -name '*.db' -o -name '*.db-*' \) -delete
find "$plugin" -depth -mindepth 1 -type d -empty -delete
cp -a -- "$staged/." "$plugin/"
mkdir -p -- "$home"
if [[ ! -e "$home/settings.yaml" && ! -L "$home/settings.yaml" ]]; then
  cp -- "$project/docs/settings.example.yaml" "$home/settings.yaml"
fi

quote_json() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  value=${value//$'\b'/\\b}
  value=${value//$'\f'/\\f}
  printf '"%s"' "$value"
}
printf 'import { setupV2 } from "./v2.mjs"\n\nexport default { id: "skill-learn", setup: ctx => setupV2(ctx, { home: %s, python: %s, ...ctx.options }) }\n' \
  "$(quote_json "$home")" "$(quote_json "$python")" > "$plugin/.index.js"
mv -- "$plugin/.index.js" "$plugin/index.js"
printf 'Installed %s\nHome: %s\nRestart the OpenCode service to load it.\n' "$plugin/index.js" "$home"
