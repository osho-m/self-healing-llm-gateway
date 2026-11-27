#!/usr/bin/env bash
# Publish the current main tree to the Hugging Face Space as one fast-forward commit.
#
# The Space needs YAML metadata at the top of README.md; GitHub renders that block as a table,
# so the GitHub README has none. This script builds the Space tree from origin/main with
# deploy/huggingface/space-header.md prepended to README.md, using a temporary git index
# (no working-tree changes), and commits it on top of the Space's current history.
#
#   SPACE_AUTHOR="Name <id@users.noreply.huggingface.co>" scripts/sync_space.sh [--push]
set -euo pipefail

SPACE_URL="${SPACE_URL:-https://huggingface.co/spaces/oshom/self-healing-llm-gateway}"
AUTHOR="${SPACE_AUTHOR:?set SPACE_AUTHOR, e.g. 'Name <id@users.noreply.huggingface.co>'}"
AUTHOR_NAME="${AUTHOR% <*}"
AUTHOR_EMAIL="${AUTHOR##*<}"; AUTHOR_EMAIL="${AUTHOR_EMAIL%>}"

git fetch -q origin main
git fetch -q "$SPACE_URL" main:refs/remotes/space/main

readme="$(git show origin/main:deploy/huggingface/space-header.md; git show origin/main:README.md)"
readme_blob="$(printf '%s\n' "$readme" | git hash-object -w --stdin)"

index="$(mktemp)"
trap 'rm -f "$index"' EXIT
GIT_INDEX_FILE="$index" git read-tree origin/main
GIT_INDEX_FILE="$index" git update-index --cacheinfo "100644,$readme_blob,README.md"
tree="$(GIT_INDEX_FILE="$index" git write-tree)"

if [ "$tree" = "$(git rev-parse refs/remotes/space/main^{tree})" ]; then
  echo "Space already matches origin/main ($(git rev-parse --short origin/main)); nothing to do."
  exit 0
fi

commit="$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
  GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
  git commit-tree "$tree" -p refs/remotes/space/main \
  -m "Sync from GitHub main $(git rev-parse --short origin/main)")"
git branch -f space-sync "$commit"
echo "Prepared space-sync at $(git rev-parse --short "$commit") on top of $(git rev-parse --short refs/remotes/space/main)."

if [ "${1:-}" = "--push" ]; then
  git push "$SPACE_URL" space-sync:main
fi
