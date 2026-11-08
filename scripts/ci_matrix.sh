#!/usr/bin/env bash
# Run the CI `test` job matrix locally: pytest+coverage, ruff, and the flakeforge
# self-check on each Python version (mirrors .github/workflows/ci.yml).
#
# Usage:
#   scripts/ci_matrix.sh                 # all versions: 3.11 3.12 3.13 3.14
#   scripts/ci_matrix.sh 3.12 3.14       # only selected versions
#   scripts/ci_matrix.sh --no-install    # reuse existing venvs without reinstalling
#   scripts/ci_matrix.sh --build         # also run `python -m build` like CI
#   scripts/ci_matrix.sh --fail-fast     # stop at the first failing version
#
# Requires `uv` (https://docs.astral.sh/uv/). Missing interpreters are downloaded
# by uv automatically. Venvs live in .ci-venvs/py<version>/ (git-ignored).
#
# Each run writes logs to .ci-venvs/logs/<UTC timestamp>/:
#   trace.jsonl                 run/version/step start+end events (exit code, duration)
#   py<ver>.log                 full output of every step for that version
#   py<ver>.telemetry.jsonl     flakeforge fault-telemetry events from the self-check

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ROOT="$ROOT/.ci-venvs"
DEFAULT_VERSIONS=(3.11 3.12 3.13 3.14)

versions=()
install=1
build=0
fail_fast=0

for arg in "$@"; do
  case "$arg" in
    --no-install) install=0 ;;
    --build) build=1 ;;
    --fail-fast) fail_fast=1 ;;
    -h|--help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    3.*) versions+=("$arg") ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
[ ${#versions[@]} -eq 0 ] && versions=("${DEFAULT_VERSIONS[@]}")

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required (brew install uv / pipx install uv)" >&2
  exit 2
fi

if [ -t 1 ]; then
  BOLD=$'\e[1m'; GREEN=$'\e[32m'; RED=$'\e[31m'; RESET=$'\e[0m'
else
  BOLD=""; GREEN=""; RED=""; RESET=""
fi

LOG_DIR="$VENV_ROOT/logs/$(date -u +%Y%m%dT%H%M%SZ)"
TRACE="$LOG_DIR/trace.jsonl"
mkdir -p "$LOG_DIR" || exit 2

# Wall-clock seconds with millisecond precision (portable: macOS date has no %N).
now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# trace <event> [key=value ...]: append one JSON event to trace.jsonl. Only the
# known measurement keys are JSON numbers; everything else (notably "python",
# where 3.10 must not become 3.1) is a string. Values must not contain quotes.
trace() {
  local line="{\"ts\":\"$(iso_now)\",\"event\":\"$1\""
  shift
  local kv key val
  for kv in "$@"; do
    key="${kv%%=*}"; val="${kv#*=}"
    if [[ "$key" =~ ^(exit_code|duration_s|events|rejected)$ ]]; then
      line+=",\"$key\":$val"
    else
      line+=",\"$key\":\"$val\""
    fi
  done
  echo "$line}" >> "$TRACE"
}

cd "$ROOT" || exit 2
summary=()
overall=0
run_start="$(now)"
trace run_start "versions=${versions[*]}" "log_dir=$LOG_DIR"

# run_step <label> <cmd...>: run a step with output teed to the version log,
# record step_start/step_end trace events, and return the step's exit status.
run_step() {
  local label="$1"; shift
  local start rc
  echo "${BOLD}--- [$py] $label${RESET}" | tee -a "$log"
  trace step_start "python=$py" "step=$label"
  start="$(now)"
  "$@" 2>&1 | tee -a "$log"
  rc=${PIPESTATUS[0]}
  trace step_end "python=$py" "step=$label" "exit_code=$rc" \
    "status=$([ "$rc" -eq 0 ] && echo pass || echo fail)" \
    "duration_s=$(perl -e "printf '%.3f', $(now) - $start")"
  return "$rc"
}

for py in "${versions[@]}"; do
  echo
  echo "${BOLD}==================== Python $py ====================${RESET}"
  venv="$VENV_ROOT/py$py"
  log="$LOG_DIR/py$py.log"
  telemetry="$LOG_DIR/py$py.telemetry.jsonl"
  failed=""
  version_start="$(now)"
  trace version_start "python=$py"

  if [ ! -x "$venv/bin/python" ]; then
    run_step "create venv" uv venv --quiet --python "$py" "$venv" || failed="venv"
    install=1
  fi

  if [ -z "$failed" ] && [ "$install" -eq 1 ]; then
    run_step "install .[dev]" uv pip install --quiet --python "$venv/bin/python" -e ".[dev]" \
      || failed="install"
  fi

  if [ -z "$failed" ]; then
    trace interpreter "python=$py" "version=$("$venv/bin/python" -c 'import platform; print(platform.python_version())')"
    # Separate coverage data file per version so runs don't clobber ./.coverage.
    export COVERAGE_FILE="$venv/.coverage"
    PATH="$venv/bin:$PATH"
    run_step "pytest" "$venv/bin/python" -m pytest -rw --cov=flakeforge --cov-report=term-missing \
      || failed="${failed:+$failed,}pytest"
    run_step "ruff" "$venv/bin/python" -m ruff check . || failed="${failed:+$failed,}ruff"
    run_step "flakeforge" "$venv/bin/python" -m flakeforge check \
      --telemetry-sink "file:$telemetry" src \
      || failed="${failed:+$failed,}flakeforge"
    if [ -f "$telemetry" ]; then
      trace telemetry "python=$py" "file=$telemetry" \
        "events=$(wc -l < "$telemetry" | tr -d ' ')" \
        "rejected=$(grep -c '"outcome": *"rejected"' "$telemetry")"
    fi
    if [ "$build" -eq 1 ]; then
      run_step "build" "$venv/bin/python" -m build --outdir "$venv/dist" \
        || failed="${failed:+$failed,}build"
    fi
    PATH="${PATH#"$venv/bin:"}"
    unset COVERAGE_FILE
  fi

  version_duration="$(perl -e "printf '%.3f', $(now) - $version_start")"
  if [ -z "$failed" ]; then
    trace version_end "python=$py" "status=pass" "duration_s=$version_duration"
    summary+=("${GREEN}PASS${RESET}  Python $py  (${version_duration}s)")
  else
    trace version_end "python=$py" "status=fail" "failed=$failed" "duration_s=$version_duration"
    summary+=("${RED}FAIL${RESET}  Python $py  ($failed)")
    overall=1
    if [ "$fail_fast" -eq 1 ]; then
      break
    fi
  fi
done

trace run_end "exit_code=$overall" "duration_s=$(perl -e "printf '%.3f', $(now) - $run_start")"

echo
echo "${BOLD}==================== Summary ====================${RESET}"
printf '%s\n' "${summary[@]}"
echo "Logs: $LOG_DIR"
exit "$overall"
