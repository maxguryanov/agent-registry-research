#!/usr/bin/env bash
# Rebuild docs/ from the database and commit it, surviving a branch that moved
# underneath us.
#
# The nightly job runs for about ninety minutes and pushes at the end. Anything
# else landing on main during that window makes the push a non-fast-forward and
# would fail the whole run after all the work was done.
#
# Recovery is by regeneration, not by merging. Everything under docs/ is derived
# from the database, so the correct resolution of any conflict is always "build
# it again on top of whatever main is now". A three-way merge of generated JSON
# would be both harder and wrong.
set -euo pipefail

MESSAGE="${1:-Publish figures for $(date -u +%Y-%m-%d)}"
BRANCH="${GITHUB_REF_NAME:-main}"
ATTEMPTS=3

if [ -z "${PYTHON:-}" ]; then
    if command -v python >/dev/null 2>&1; then PYTHON=python; else PYTHON=python3; fi
fi

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Returns 0 when there is something to push, 1 when the figures are unchanged.
# A failure to build them is not either of those: it exits the script, because
# `set -e` does not apply inside a function called as a condition, and a
# swallowed build failure would look exactly like a quiet night.
stage_and_commit() {
    if ! "$PYTHON" -m monitor.publish; then
        echo "::error title=Publish failed::monitor.publish did not complete. \
The measurements are in the database; the page was not rebuilt."
        exit 1
    fi
    if [ ! -f docs/index.html ]; then
        echo "::error title=Publish failed::monitor.publish reported success \
but produced no docs/index.html."
        exit 1
    fi
    git add docs
    if git diff --staged --quiet; then
        echo "figures unchanged, nothing to commit"
        return 1
    fi
    git commit -m "$MESSAGE"
    return 0
}

if ! stage_and_commit; then
    exit 0
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
    if git push origin "HEAD:$BRANCH"; then
        echo "published on attempt $attempt"
        exit 0
    fi
    if [ "$attempt" -eq "$ATTEMPTS" ]; then
        break
    fi
    echo "push rejected: $BRANCH moved while this run was working."
    echo "rebuilding on top of the new tip and trying again."
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
    if ! stage_and_commit; then
        exit 0                 # someone else published identical figures
    fi
done

echo "::error title=Publish failed::Could not push after $ATTEMPTS attempts. \
The measurements are safely in the database; re-run the publish-now task."
exit 1
