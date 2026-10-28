#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# The one script every CI template runs (ADR-0058, E3). Each template carries a copy of the
# block between the two marker lines (GitLab, CircleCI and Azure DevOps inline it, the Jenkins
# library ships it as a resource, the Bitbucket pipe as its entrypoint), and
# tests/test_ci_templates.py keeps every copy identical to this file.
#
# Inputs, all through the environment (never pasted into the script):
#   SUITE, PROFILE, TARGET          required
#   TRANSPORT (websocket), REPEATS, FILTER (one per line), EXTRA_ARGS (space-separated flags)
#   ALLOWED_HOSTS, SIMULATOR_BASE_URL, SIMULATOR_MODEL, JUDGE_BASE_URL, JUDGE_MODEL
#   OPSEVAL_ENV                     other non-secret settings, one OPSEVAL_NAME=value per line
#   READ_DOTENV (false)             "true" lets Opseval read a .env in the workspace
#   MODE (local)                    local: `opseval` is on PATH (the job runs in the image);
#                                   docker: run the image with `docker run`
#   IMAGE                           the image, for MODE=docker
#   REPORTS_DIR (opseval-reports), JUNIT_PATH ($REPORTS_DIR/junit.xml)
# Secrets come from the CI system's secret store, already in the environment under their
# own names: OPSEVAL_SIMULATOR_API_KEY, OPSEVAL_JUDGE_API_KEY, OPSEVAL_STATIC_TOKEN. With
# MODE=docker they reach the container by name (`-e NAME`), never on the command line.
#
# The exit code is Opseval's: 0 pass, 1 the agent failed, 2 not judged, 3 harness or
# configuration error. Only 0 passes. Anything else (docker could not start the image, an
# unexpected code) is reported as 3, and an exit 2 that left no result is Opseval refusing its
# arguments, which is a configuration error (3), not "not judged".
#
# Written for bash 3.2+ and to run under `set -e` as well. No heredocs (CircleCI reads "<<"),
# and every command substitution has a space in it (Azure DevOps expands "$(name)").

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
for oe_name in SUITE PROFILE TARGET; do
  if [ -z "${!oe_name:-}" ]; then oe_fail "$oe_name is required"; fi
done
case "${READ_DOTENV:-false}" in
  true) oe_dotenv=true ;;
  false | "") oe_dotenv=false ;;
  *) oe_fail "READ_DOTENV must be true or false" ;;
esac
# Secret settings (config.SECRET_FIELDS): only ever from the CI system's secret store.
oe_secret=" OPSEVAL_STATIC_TOKEN OPSEVAL_TOKEN_SIGNING_KEY OPSEVAL_SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY OPSEVAL_TTS_KEY OPSEVAL_STT_KEY OPSEVAL_STT_SECOND_KEY OPSEVAL_AUDIO_ENCRYPTION_KEY OPSEVAL_LANGFUSE_SECRET_KEY OPSEVAL_SERVE_TOKEN OPSEVAL_WEBHOOK_URL OPSEVAL_GITHUB_TOKEN OPSEVAL_JIRA_TOKEN OPSEVAL_DATABASE_URL "
# Settings with their own input.
oe_own=" OPSEVAL_ALLOWED_HOSTS OPSEVAL_SIMULATOR_BASE_URL OPSEVAL_SIMULATOR_MODEL OPSEVAL_JUDGE_BASE_URL OPSEVAL_JUDGE_MODEL OPSEVAL_DOTENV "
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
oe_args=(run "$SUITE" --profile "$PROFILE" --target "$TARGET" --transport "${TRANSPORT:-websocket}")
if [ -n "${REPEATS:-}" ]; then oe_args+=(--repeats "$REPEATS"); fi
while IFS= read -r oe_f || [ -n "$oe_f" ]; do
  if [ -n "$oe_f" ]; then oe_args+=(--filter "$oe_f"); fi
done < <(printf '%s' "${FILTER:-}")
# Word-split on purpose: these are flags, not a shell command.
oe_extra_line="${EXTRA_ARGS:-}"
read -r -a oe_extra < <(printf '%s\n' "${oe_extra_line//$'\n'/ }")
oe_reports="${REPORTS_DIR:-opseval-reports}"
oe_junit="${JUNIT_PATH:-$oe_reports/junit.xml}"
if ! mkdir -p "$oe_reports"; then
  oe_fail "cannot create the reports folder '$oe_reports' (is the workspace writable by uid $(id -u)?)"
fi
# Nothing from an earlier run in a reused workspace may be published as this run's.
rm -f "$oe_junit" "$oe_reports/summary.md" "$oe_reports/run-dir"
if ! oe_out=$(mktemp -d "$oe_reports/run-XXXXXX"); then
  oe_fail "cannot create a run folder in '$oe_reports'"
fi
oe_args+=(--reports-dir "$oe_out" --junit "$oe_out/junit.xml")
if [ "$oe_mode" = docker ]; then
  if [ -z "${IMAGE:-}" ]; then oe_fail "IMAGE is required with MODE=docker"; fi
  case "$IMAGE" in
    *"<version>"*) oe_fail "IMAGE is the placeholder '$IMAGE': until the image is published, build it with 'docker build -t opseval .' in the Opseval repository and set IMAGE=opseval" ;;
  esac
  if ! oe_envfile=$(mktemp "${TMPDIR:-/tmp}/opseval-env.XXXXXX"); then oe_fail "cannot create a temporary file"; fi
  trap 'rm -f "$oe_envfile"' EXIT
  printf '%s\n' "${oe_settings[@]}" > "$oe_envfile"
  # The container runs as this job's user, so it can write the reports into the workspace.
  # Never as root: a job running as root runs it as the image's own user (10001).
  oe_uid=$(id -u)
  if [ "$oe_uid" = 0 ]; then
    oe_user=10001:10001
    chmod 0777 "$oe_out"
  else
    oe_user="$oe_uid:$(id -g)"
  fi
  oe_cmd=(docker run --rm --user "$oe_user" -e HOME=/tmp -v "$PWD:/work" -w /work --env-file "$oe_envfile")
  for oe_s in OPSEVAL_SIMULATOR_API_KEY OPSEVAL_JUDGE_API_KEY OPSEVAL_STATIC_TOKEN; do
    if [ -n "${!oe_s:-}" ]; then oe_cmd+=(-e "$oe_s"); fi
  done
  oe_cmd+=("$IMAGE")
else
  unset OPSEVAL_ENV
  for oe_s in "${oe_settings[@]}"; do export "${oe_s?}"; done
  oe_cmd=(opseval)
fi
oe_code=0
"${oe_cmd[@]}" "${oe_args[@]}" ${oe_extra[@]+"${oe_extra[@]}"} || oe_code=$?
oe_run_dir=$(find "$oe_out" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -f "$oe_out/junit.xml" ]; then
  mkdir -p "$(dirname "$oe_junit")" && cp "$oe_out/junit.xml" "$oe_junit" ||
    echo "Opseval: warning: could not copy the JUnit file to $oe_junit; the exit code still stands." >&2
fi
if [ -n "$oe_run_dir" ]; then printf '%s\n' "$oe_run_dir" > "$oe_reports/run-dir"; fi
if [ -n "$oe_run_dir" ] && [ -f "$oe_run_dir/result.json" ]; then
  # Reporting never decides: a summary that cannot be written is a warning.
  if ! "${oe_cmd[@]}" summary "$oe_run_dir" > "$oe_reports/summary.md"; then
    echo "Opseval: warning: could not write the gate summary; the exit code still stands." >&2
  fi
elif [ "$oe_code" = 2 ]; then
  echo "Opseval: exit 2 with no result: Opseval refused its arguments (see above)." >&2
  oe_code=3
fi
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
