#!/usr/bin/env bash
# Tag a recorded milestone and publish it as a GitHub release with its videos.
# Run it from your own computer: cloud sessions may not be allowed to push tags or
# create releases.
#
#   ./scripts/publish_milestone.sh 2026-11-06-stage0-01-airport-data
#
# The diary entry must already be committed. This script then:
#   1. tags the milestone's commit with its name (e.g. stage0-01-airport-data)
#   2. pushes the tag
#   3. creates a GitHub release for the tag and uploads the videos from the diary folder
set -euo pipefail

FOLDER="${1:?usage: publish_milestone.sh <diary folder, e.g. 2026-11-06-stage0-01-airport-data>}"
META="diary/$FOLDER/milestone.json"
[ -f "$META" ] || { echo "No $META: run python -m approach.milestone first" >&2; exit 1; }
SLUG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['slug'])" "$META")
TITLE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['title'])" "$META")

if [ -n "$(git status --porcelain -- diary)" ]; then
  echo "diary/ has uncommitted changes: commit them first" >&2
  exit 1
fi

REPO=$(gh api "repos/$(git remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')" --jq .full_name)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

SHA=$(git log -1 --format=%H -- "diary/$FOLDER")
git tag -a "$SLUG" -m "$TITLE" "$SHA" 2>/dev/null || echo "Tag $SLUG already exists locally"
git push origin "$BRANCH"
# The release below creates the tag on GitHub at this commit if it isn't there yet,
# so a failed tag push (some environments only allow branch pushes) is not fatal.
git push origin "$SLUG" 2>/dev/null || echo "Tag push refused; the release will create the tag on GitHub"

BODY="$TITLE

Diary entry: https://github.com/$REPO/blob/$SLUG/diary/$FOLDER/README.md

Videos are attached below. The radar replays in the diary folder open in any browser."
RELEASE_ID=$(gh api "repos/$REPO/releases/tags/$SLUG" --jq .id 2>/dev/null | grep -E '^[0-9]+$' || true)
if [ -z "$RELEASE_ID" ]; then
  RELEASE_ID=$(gh api -X POST "repos/$REPO/releases" -f tag_name="$SLUG" -f target_commitish="$SHA" \
    -f name="$TITLE" -f body="$BODY" --jq .id)
fi

shopt -s nullglob
for f in diary/"$FOLDER"/*.mp4; do
  name=$(basename "$f")
  type=video/mp4; [[ "$name" == *.webm ]] && type=video/webm
  echo "Uploading $name"
  gh api -X POST "https://uploads.github.com/repos/$REPO/releases/$RELEASE_ID/assets?name=$name" \
    -H "Content-Type: $type" --input "$f" --jq .browser_download_url
done
echo "Release: https://github.com/$REPO/releases/tag/$SLUG"
