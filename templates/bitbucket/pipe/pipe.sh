#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# The Opseval Bitbucket pipe's entrypoint (ADR-0058, E3). Bitbucket passes the pipe's
# variables as environment variables; the shared CI script (templates/shared/opseval-ci.sh,
# copied below by scripts/ci_templates.py) reads them. The pipe runs inside the Opseval
# image, so Opseval runs directly (MODE=local), as the image's non-root user (10001).
export REPORTS_DIR="${REPORTS_DIR:-opseval-reports}"
# Bitbucket shows test reports it finds under test-results/; the run folder stays elsewhere
# so its own copy of junit.xml is not counted twice.
export JUNIT_PATH="${JUNIT_PATH:-test-results/opseval/junit.xml}"
# Bitbucket mounts the clone owned by root, which uid 10001 cannot write. So the pipe starts
# as root for this alone: it makes the two report folders, gives them to 10001 (never 0777),
# and runs the rest of this script as 10001 with setpriv (util-linux, checked when the pipe
# image is built). Opseval itself never runs as root.
if [ "$(id -u)" = 0 ]; then
  if [ -n "${OE_PIPE_DROPPED:-}" ]; then
    echo "Opseval: the pipe is still root after dropping to uid 10001 (exit 3, a harness error)" >&2
    exit 3
  fi
  oe_junit_dir=$(dirname "$JUNIT_PATH")
  if ! mkdir -p "$REPORTS_DIR" "$oe_junit_dir" || ! chown 10001:10001 "$REPORTS_DIR" "$oe_junit_dir"; then
    echo "Opseval: cannot prepare the report folders for uid 10001 (exit 3, a harness error)" >&2
    exit 3
  fi
  exec setpriv --reuid=10001 --regid=10001 --clear-groups env OE_PIPE_DROPPED=1 HOME=/home/opseval bash "$0" "$@"
fi
# Secret variables are mapped to Opseval's own names here, so a pipeline passes them as
# SIMULATOR_API_KEY / JUDGE_API_KEY / AGENT_TOKEN (and the server key as PUBLIC_KEY /
# SECRET_KEY) from secured repository variables. Other secret settings are passed under their
# own names and listed in SECRET_ENV_NAMES.
for oe_pair in OPSEVAL_SIMULATOR_API_KEY:SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY:JUDGE_API_KEY \
  OPSEVAL_STATIC_TOKEN:AGENT_TOKEN OPSEVAL_PUBLIC_KEY:PUBLIC_KEY OPSEVAL_SECRET_KEY:SECRET_KEY; do
  oe_to="${oe_pair%%:*}"
  oe_from="${oe_pair#*:}"
  if [ -n "${!oe_from:-}" ]; then export "$oe_to=${!oe_from}"; else unset "$oe_to"; fi
  unset "$oe_from"
done
export MODE=local
# --- begin opseval-ci (a copy of templates/shared/opseval-ci.sh; keep every copy identical)
set -uo pipefail
oe_fail() {
  echo "Opseval: $1 (exit 3, a configuration error)" >&2
  exit 3
}
oe_mode="${MODE:-local}"
case "$oe_mode" in
  local | docker) ;;
  *) oe_fail "MODE must be local or docker, not '$oe_mode'" ;;
esac
for oe_name in SUITE TARGET; do
  if [ -z "${!oe_name:-}" ]; then oe_fail "$oe_name is required"; fi
done
if [ -z "${PROFILE:-}" ] && [ -z "${SERVER_URL:-}" ]; then oe_fail "PROFILE is required"; fi
# Reporting to a server (ADR-0058 E2). A project without a server would report nothing while
# the pipeline's owner believes it did.
for oe_name in SERVER_URL PROJECT TASK LABEL; do
  case "${!oe_name:-}" in
    *$'\n'* | *$'\r'*) oe_fail "the value for $oe_name has more than one line" ;;
  esac
done
oe_server=()
if [ -n "${SERVER_URL:-}" ]; then
  if [ -z "${PROJECT:-}" ]; then oe_fail "SERVER_URL needs PROJECT (org/project)"; fi
  oe_server=(--server "$SERVER_URL" --project "$PROJECT" --task "${TASK:-default}" --label "${LABEL:-production}")
elif [ -n "${PROJECT:-}" ]; then
  oe_fail "PROJECT needs SERVER_URL: without a server nothing would be reported"
fi
case "${READ_DOTENV:-false}" in
  true) oe_dotenv=true ;;
  false | "") oe_dotenv=false ;;
  *) oe_fail "READ_DOTENV must be true or false" ;;
esac
# Secret settings (config.SECRET_FIELDS): only ever from the CI system's secret store.
oe_secret=" OPSEVAL_STATIC_TOKEN OPSEVAL_TOKEN_SIGNING_KEY OPSEVAL_SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY OPSEVAL_TTS_KEY OPSEVAL_STT_KEY OPSEVAL_STT_SECOND_KEY OPSEVAL_AUDIO_ENCRYPTION_KEY OPSEVAL_LANGFUSE_SECRET_KEY OPSEVAL_SERVE_TOKEN OPSEVAL_OIDC_CLIENT_SECRET OPSEVAL_WEBHOOK_URL OPSEVAL_GITHUB_TOKEN OPSEVAL_JIRA_TOKEN OPSEVAL_DATABASE_URL OPSEVAL_S3_ACCESS_KEY_ID OPSEVAL_S3_SECRET_ACCESS_KEY OPSEVAL_SECRET_KEY "
# Settings with their own input.
oe_own=" OPSEVAL_ALLOWED_HOSTS OPSEVAL_SIMULATOR_BASE_URL OPSEVAL_SIMULATOR_MODEL OPSEVAL_JUDGE_BASE_URL OPSEVAL_JUDGE_MODEL OPSEVAL_DOTENV OPSEVAL_PUBLIC_KEY "
oe_settings=("OPSEVAL_DOTENV=$oe_dotenv")
for oe_pair in "OPSEVAL_ALLOWED_HOSTS=${ALLOWED_HOSTS:-}" \
  "OPSEVAL_SIMULATOR_BASE_URL=${SIMULATOR_BASE_URL:-}" "OPSEVAL_SIMULATOR_MODEL=${SIMULATOR_MODEL:-}" \
  "OPSEVAL_JUDGE_BASE_URL=${JUDGE_BASE_URL:-}" "OPSEVAL_JUDGE_MODEL=${JUDGE_MODEL:-}"; do
  oe_name="${oe_pair%%=*}"
  case "$oe_pair" in
    *$'\n'* | *$'\r'*) oe_fail "the value for $oe_name has more than one line" ;;
  esac
  if [ -n "${oe_pair#*=}" ]; then oe_settings+=("$oe_pair"); fi
done
oe_n=0
while IFS= read -r oe_line || [ -n "$oe_line" ]; do
  oe_n=$((oe_n + 1))
  if [ -z "${oe_line//[[:space:]]/}" ] || [[ "$oe_line" =~ ^[[:space:]]*# ]]; then continue; fi
  case "$oe_line" in
    *$'\r'*) oe_fail "OPSEVAL_ENV line $oe_n has a carriage return" ;;
  esac
  if [[ ! "$oe_line" =~ ^OPSEVAL_[A-Z0-9_]+= ]]; then
    oe_fail "OPSEVAL_ENV line $oe_n is not OPSEVAL_NAME=value"
  fi
  oe_name="${oe_line%%=*}"
  if [[ "$oe_secret" == *" $oe_name "* ]]; then
    oe_fail "OPSEVAL_ENV line $oe_n sets $oe_name, a secret: put it in the CI system's secret store instead"
  fi
  if [[ "$oe_own" == *" $oe_name "* ]]; then
    oe_fail "OPSEVAL_ENV line $oe_n sets $oe_name, which has its own input: use that"
  fi
  for oe_seen in "${oe_settings[@]}"; do
    if [ "${oe_seen%%=*}" = "$oe_name" ]; then oe_fail "OPSEVAL_ENV line $oe_n sets $oe_name twice"; fi
  done
  oe_settings+=("$oe_line")
done < <(printf '%s' "${OPSEVAL_ENV:-}")
# The secrets passed on: those with their own input, and the others named (never valued).
oe_fixed=" OPSEVAL_SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY OPSEVAL_STATIC_TOKEN OPSEVAL_PUBLIC_KEY OPSEVAL_SECRET_KEY "
oe_forward=(OPSEVAL_SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY OPSEVAL_STATIC_TOKEN OPSEVAL_PUBLIC_KEY OPSEVAL_SECRET_KEY)
oe_named_line="${SECRET_ENV_NAMES:-}"
oe_named_line="${oe_named_line//$'\n'/ }"
oe_named_line="${oe_named_line//$'\r'/ }"
oe_named_line="${oe_named_line//,/ }"
read -r -a oe_named < <(printf '%s\n' "$oe_named_line")
for oe_name in ${oe_named[@]+"${oe_named[@]}"}; do
  if [[ "$oe_fixed" == *" $oe_name "* ]]; then
    oe_fail "SECRET_ENV_NAMES lists $oe_name, which has its own input: use that"
  fi
  if [[ "$oe_secret" != *" $oe_name "* ]]; then
    oe_fail "SECRET_ENV_NAMES lists '$oe_name', which is not a secret setting: put a non-secret setting in OPSEVAL_ENV"
  fi
  if [[ " ${oe_forward[*]} " == *" $oe_name "* ]]; then continue; fi
  oe_forward+=("$oe_name")
  if [ -z "${!oe_name:-}" ]; then
    echo "Opseval: warning: SECRET_ENV_NAMES lists $oe_name, but this job has no value for it." >&2
  fi
done
# The value a setting will have: ours, else (MODE=local) the job's own environment.
oe_value() {
  for oe_s in "${oe_settings[@]}"; do
    if [ "${oe_s%%=*}" = "$1" ]; then
      printf '%s' "${oe_s#*=}"
      return 0
    fi
  done
  if [ "$oe_mode" = local ]; then printf '%s' "${!1:-}"; fi
}
# A key with no endpoint and model to use it on would be silently unused: the run would skip
# LLM personas or leave judges unmeasured while the pipeline's owner believes they ran.
for oe_role in SIMULATOR JUDGE; do
  oe_key="OPSEVAL_${oe_role}_API_KEY"
  if [ -n "${!oe_key:-}" ]; then
    if [ -z "$(oe_value "OPSEVAL_${oe_role}_BASE_URL")" ] || [ -z "$(oe_value "OPSEVAL_${oe_role}_MODEL")" ]; then
      oe_fail "$oe_key is set but OPSEVAL_${oe_role}_BASE_URL and OPSEVAL_${oe_role}_MODEL are not both set: the key would be unused"
    fi
  fi
done
oe_args=(run "$SUITE")
if [ -n "${PROFILE:-}" ]; then oe_args+=(--profile "$PROFILE"); fi
oe_args+=(--target "$TARGET" --transport "${TRANSPORT:-websocket}" ${oe_server[@]+"${oe_server[@]}"})
if [ -n "${REPEATS:-}" ]; then oe_args+=(--repeats "$REPEATS"); fi
while IFS= read -r oe_f || [ -n "$oe_f" ]; do
  if [ -n "$oe_f" ]; then oe_args+=(--filter "$oe_f"); fi
done < <(printf '%s' "${FILTER:-}")
# Word-split on purpose: these are flags, not a shell command.
oe_extra_line="${EXTRA_ARGS:-}"
read -r -a oe_extra < <(printf '%s\n' "${oe_extra_line//$'\n'/ }")
for oe_a in ${oe_extra[@]+"${oe_extra[@]}"}; do
  case "$oe_a" in
    --help | --version) oe_fail "EXTRA_ARGS has $oe_a: Opseval would print and exit without running, and nothing would be judged" ;;
    --reports-dir | --reports-dir=* | --junit | --junit=*) oe_fail "EXTRA_ARGS has $oe_a, which the template sets itself" ;;
  esac
done
oe_reports="${REPORTS_DIR:-opseval-reports}"
oe_junit="${JUNIT_PATH:-$oe_reports/junit.xml}"
# MODE=docker: the container sees the workspace only, mounted at the same absolute path, so
# every path Opseval is given must be inside it.
oe_ws=$(pwd -P)
oe_inside() {
  local oe_p="$1" oe_d
  case "$oe_p" in
    /*) ;;
    *) oe_p="$PWD/$oe_p" ;;
  esac
  if [ -d "$oe_p" ]; then oe_d="$oe_p"; else oe_d=$(dirname "$oe_p"); fi
  if oe_d=$(cd "$oe_d" 2>/dev/null && pwd -P); then
    case "$oe_d/" in "$oe_ws"/*) return 0 ;; esac
    return 1
  fi
  # Not there (yet): decide on the text.
  case "/$oe_p/" in */../*) return 1 ;; esac
  case "$oe_p" in "$PWD"/* | "$oe_ws"/*) return 0 ;; esac
  return 1
}
if [ "$oe_mode" = docker ]; then
  if [ -z "${IMAGE:-}" ]; then oe_fail "IMAGE is required with MODE=docker"; fi
  case "$IMAGE" in
    *"<version>"*) oe_fail "IMAGE is the placeholder '$IMAGE': until the image is published, build it with 'docker build -t opseval .' in the Opseval repository and set IMAGE=opseval" ;;
  esac
  oe_host="${DOCKER_WORKSPACE_HOST_PATH:-$PWD}"
  case "$oe_host" in
    /*) ;;
    *) oe_fail "DOCKER_WORKSPACE_HOST_PATH must be an absolute path, not '$oe_host'" ;;
  esac
  case "$oe_host$PWD" in
    *:* | *$'\n'*) oe_fail "the workspace path has a ':' or a line break, which docker run -v cannot mount" ;;
  esac
  for oe_name in SUITE PROFILE REPORTS_DIR; do
    oe_p="${!oe_name:-}"
    if [ "$oe_name" = REPORTS_DIR ]; then oe_p="$oe_reports"; fi
    # With a server, SUITE is a path inside the catalogue Opseval pulls into the container.
    if [ "$oe_name" = SUITE ] && [ -n "${SERVER_URL:-}" ]; then continue; fi
    case "$oe_p" in "" | builtin:*) continue ;; esac
    if ! oe_inside "$oe_p"; then
      oe_fail "$oe_name '$oe_p' is outside the workspace ($PWD), the only folder the container sees"
    fi
  done
fi
if ! mkdir -p "$oe_reports"; then
  oe_fail "cannot create the reports folder '$oe_reports' (is the workspace writable by uid $(id -u)?)"
fi
# Nothing from an earlier build in a reused workspace may be published as this one's: its
# files go, and so do its run folders (only those this script made and marked, never a link).
rm -f "$oe_junit" "$oe_reports/summary.md" "$oe_reports/run-dir"
oe_marker=.opseval-ci-build
for oe_old in "$oe_reports"/build-*; do
  if [ -d "$oe_old" ] && [ ! -L "$oe_old" ] && [ -f "$oe_old/$oe_marker" ]; then
    if ! rm -rf "$oe_old"; then oe_fail "cannot remove an earlier build's run folder '$oe_old'"; fi
  fi
done
if ! oe_out=$(mktemp -d "$oe_reports/build-XXXXXX"); then
  oe_fail "cannot create a run folder in '$oe_reports'"
fi
: > "$oe_out/$oe_marker"
oe_args+=(--reports-dir "$oe_out" --junit "$oe_out/junit.xml")
if [ "$oe_mode" = docker ]; then
  if ! oe_envfile=$(mktemp "${TMPDIR:-/tmp}/opseval-env.XXXXXX"); then oe_fail "cannot create a temporary file"; fi
  trap 'rm -f "$oe_envfile"' EXIT
  printf '%s\n' "${oe_settings[@]}" > "$oe_envfile"
  # The container runs as this job's user, so it can write the reports into the workspace.
  # Never as root: a job running as root runs it as the image's own user (10001) and gives
  # that user this build's run folder alone (never a world-writable folder).
  oe_uid=$(id -u)
  if [ "$oe_uid" = 0 ]; then
    oe_user=10001:10001
    if ! chown -R 10001:10001 "$oe_out"; then oe_fail "cannot give the run folder to uid 10001"; fi
  else
    oe_user="$oe_uid:$(id -g)"
  fi
  oe_cmd=(docker run --rm --user "$oe_user" -e HOME=/tmp -v "$oe_host:$PWD" -w "$PWD" --env-file "$oe_envfile")
  for oe_s in "${oe_forward[@]}"; do
    if [ -n "${!oe_s:-}" ]; then oe_cmd+=(-e "$oe_s"); fi
  done
  oe_cmd+=("$IMAGE")
else
  unset OPSEVAL_ENV
  for oe_s in "${oe_settings[@]}"; do export "${oe_s?}"; done
  oe_cmd=(opseval)
fi
oe_code=0
if [ "${TF_BUILD:-}" = True ]; then
  # Azure DevOps runs a "##vso[" or "##[" in a step's output as a logging command, and the
  # agent's own words can reach Opseval's output: a zero-width space after the first "#" keeps
  # them text. The exit code is Opseval's, never the filter's.
  oe_zw=$(printf '\342\200\213')
  if "${oe_cmd[@]}" "${oe_args[@]}" ${oe_extra[@]+"${oe_extra[@]}"} 2>&1 |
    sed -e "s/##\([vV][sS][oO]\[\)/#$oe_zw#\1/g" -e "s/##\[/#$oe_zw#[/g"; then
    oe_code=0
  else
    oe_code=${PIPESTATUS[0]}
  fi
else
  "${oe_cmd[@]}" "${oe_args[@]}" ${oe_extra[@]+"${oe_extra[@]}"} || oe_code=$?
fi
oe_run_dir=$(find "$oe_out" -mindepth 1 -maxdepth 1 -type d | head -n 1)
oe_result=""
if [ -n "$oe_run_dir" ] && [ -f "$oe_run_dir/result.json" ]; then oe_result="$oe_run_dir/result.json"; fi
if [ -f "$oe_out/junit.xml" ]; then
  mkdir -p "$(dirname "$oe_junit")" && cp "$oe_out/junit.xml" "$oe_junit" ||
    echo "Opseval: warning: could not copy the JUnit file to $oe_junit; the exit code still stands." >&2
fi
if [ -n "$oe_run_dir" ]; then printf '%s\n' "$oe_run_dir" > "$oe_reports/run-dir"; fi
oe_recorded=""
if [ -n "$oe_result" ]; then
  # Reporting never decides: a summary that cannot be written is a warning.
  if ! "${oe_cmd[@]}" summary "$oe_run_dir" > "$oe_reports/summary.md"; then
    echo "Opseval: warning: could not write the gate summary; the exit code still stands." >&2
  fi
  # The run's own exit code: the top-level key of result.json (written with an indent of 2).
  oe_recorded=$(sed -n 's/^  "exit_code": \([0-9][0-9]*\),*$/\1/p' "$oe_result")
fi
# 0, 1 and 2 are verdicts: only a run that finished and recorded the same code can give one.
case "$oe_code" in
  0 | 1 | 2)
    if [ -z "$oe_result" ]; then
      echo "Opseval: exit $oe_code but no result.json, so nothing was judged: Opseval refused its arguments (check EXTRA_ARGS, see above), or the image's entrypoint is not opseval." >&2
      oe_code=3
    elif [ "$oe_recorded" != "$oe_code" ]; then
      echo "Opseval: exit $oe_code but the run recorded exit ${oe_recorded:-nothing readable} in $oe_result: the outcome cannot be trusted." >&2
      oe_code=3
    fi
    ;;
esac
case "$oe_code" in
  0) echo "Opseval: pass (exit 0)." ;;
  1) echo "Opseval: the agent failed (exit 1). This blocks the release." >&2 ;;
  2) echo "Opseval: not judged (exit 2). Not an agent failure, but nothing that was not judged may ship: this blocks the release." >&2 ;;
  3) echo "Opseval: harness or configuration error (exit 3). Not an agent failure; this blocks the release." >&2 ;;
  *)
    echo "Opseval: did not finish (exit $oe_code): a harness error, reported as exit 3." >&2
    oe_code=3
    ;;
esac
exit "$oe_code"
# --- end opseval-ci
