#!/usr/bin/env bash
# Download the parts of VATSIM UK's open-source sector file that this project uses
# (Gatwick airport data and navaids). The sector file is GPL-3 licensed, so it is
# fetched at build time into data/external/ and never committed to this repo.
set -euo pipefail

DEST="${1:-data/external/UK-Sector-File}"
REPO="https://github.com/VATSIM-UK/UK-Sector-File.git"

if [ -d "$DEST/.git" ]; then
  echo "Updating $DEST"
  git -C "$DEST" pull --ff-only
else
  echo "Cloning $REPO into $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 --filter=blob:none --sparse "$REPO" "$DEST"
fi

git -C "$DEST" sparse-checkout set Airports/EGKK Navaids
echo "Done: $(git -C "$DEST" log -1 --format='%h %ad' --date=short)"
ls "$DEST/Airports/EGKK"
