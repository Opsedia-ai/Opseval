#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Create or update the ruleset that protects `main` from .github/rulesets/main.json (ADR-0065).
#
#   scripts/apply_rulesets.sh                      # dry run (the default): validate, print
#   scripts/apply_rulesets.sh --dry-run            # the same, said explicitly (CI runs this)
#   scripts/apply_rulesets.sh --apply [--repo OWNER/NAME]
#
# Every mode first runs scripts/check_rulesets.py, which fails when the required status checks
# do not match the jobs in .github/workflows/ci.yml. A dry run never calls the GitHub API.
# --apply needs `gh` logged in as a repository admin. Without --repo it targets the repository
# of the checkout this script is in, not of the current directory. It finds an existing
# ruleset by its name and updates it (PUT), or creates one (POST), so running it twice
# changes nothing.
# Run it on go-public day, right after the visibility change (ATTENTION I24): GitHub refuses
# rulesets on this private repository on its current plan (HTTP 403).

set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ruleset="$root/.github/rulesets/main.json"
mode=dry-run
repo=""

usage() {
	sed -n '4,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
	case "$1" in
		--dry-run) mode=dry-run ;;
		--apply) mode=apply ;;
		--repo)
			[ $# -ge 2 ] || { echo "apply_rulesets: --repo needs OWNER/NAME" >&2; exit 2; }
			repo=$2
			shift
			;;
		-h | --help) usage; exit 0 ;;
		*) echo "apply_rulesets: unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
	shift
done

if command -v uv >/dev/null 2>&1; then
	python=(uv run --quiet --project "$root" python)
else
	python=(python3)
fi
"${python[@]}" "$root/scripts/check_rulesets.py" "$ruleset" "$root/.github/workflows/ci.yml"

name=$("${python[@]}" -c 'import json, sys; print(json.load(open(sys.argv[1]))["name"])' "$ruleset")

if [ "$mode" = dry-run ]; then
	echo "Dry run: nothing is sent. With --apply, ruleset \"$name\" would be created or updated on"
	echo "  ${repo:-the repository of this checkout, $root (gh repo view)}"
	echo "with this payload (POST or PUT repos/{owner}/{repo}/rulesets):"
	cat "$ruleset"
	exit 0
fi

command -v gh >/dev/null 2>&1 || { echo "apply_rulesets: the GitHub CLI (gh) is not installed" >&2; exit 2; }
if [ -z "$repo" ]; then
	# The repository of this script's own checkout, wherever it is run from.
	repo=$(cd "$root" && gh repo view --json nameWithOwner --jq .nameWithOwner)
fi
case "$repo" in
	*/*) ;;
	*) echo "apply_rulesets: --repo must be OWNER/NAME, got: $repo" >&2; exit 2 ;;
esac

existing=$(gh api --paginate "repos/$repo/rulesets?includes_parents=false" \
	--jq ".[] | select(.name == \"$name\" and .target == \"branch\") | .id")
count=$(printf '%s\n' "$existing" | grep -c . || true)

if [ "$count" -gt 1 ]; then
	echo "apply_rulesets: $count branch rulesets on $repo are named \"$name\"; remove the extra ones first." >&2
	exit 1
elif [ "$count" -eq 1 ]; then
	echo "Updating ruleset \"$name\" (id $existing) on $repo"
	gh api -X PUT "repos/$repo/rulesets/$existing" --input "$ruleset" --jq '"ruleset \(.id): \(.enforcement)"'
else
	echo "Creating ruleset \"$name\" on $repo"
	gh api -X POST "repos/$repo/rulesets" --input "$ruleset" --jq '"ruleset \(.id): \(.enforcement)"'
fi
