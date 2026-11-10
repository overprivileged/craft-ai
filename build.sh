#!/usr/bin/env bash
# Craft AI: build FilmCraft with the AI panel.
# Unofficial. Not made by the ArtCraft team.
#
# Clones the OFFICIAL FilmCraft at a pinned tag, checks the patch checksum, applies the patch and
# builds the app + CLI in release mode. Nothing of FilmCraft is re-hosted here: only our patch.
#
#   ./build.sh                 # builds into ./vendor/filmcraft
#   ./build.sh /some/new/dir   # builds into a folder of your choice (must not exist yet)
#   CRAFT_AI_WHISPER=1 ./build.sh   # also turn on FilmCraft's optional speech-to-text (longer build);
#                                   # the speech model itself is downloaded later, from inside the app
set -euo pipefail

REPO="https://github.com/storytold/filmcraft.git"
TAG="v0.19.0"
COMMIT="d1af238bba9fc814487c81e47b53543dff435c09"   # what $TAG pointed to when the patch was made
PATCH_SHA256="79aafe560d28417689d45f832ee928897d87de2900a958f3a6af2b3551a56676"

HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH="$HERE/patches/filmcraft-ai-panel.patch"
DEST="${1:-$HERE/vendor/filmcraft}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }

for tool in git cargo; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed (Rust: https://rustup.rs)"
done
if command -v shasum >/dev/null 2>&1; then sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
elif command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else die "need shasum or sha256sum to check the patch"; fi

say "Checking the patch"
[ -f "$PATCH" ] || die "missing $PATCH"
got="$(sha "$PATCH")"
[ "$got" = "$PATCH_SHA256" ] || die "patch checksum mismatch: expected $PATCH_SHA256, got $got"
echo "sha256 $got  ok"

[ -e "$DEST" ] && die "$DEST already exists. Remove it, or pass a new folder: ./build.sh /path/to/new/dir"

say "Cloning official FilmCraft $TAG"
git clone --quiet --depth 1 --branch "$TAG" "$REPO" "$DEST"
head="$(git -C "$DEST" rev-parse HEAD)"
[ "$head" = "$COMMIT" ] || die "$TAG is now $head, not $COMMIT. The patch was made for $COMMIT; refusing to continue."
echo "at $head  ok"

say "Applying the AI panel patch"
git -C "$DEST" apply --check "$PATCH" || die "the patch does not apply to $TAG"
git -C "$DEST" apply "$PATCH"
echo "$(git -C "$DEST" status --short | wc -l | tr -d " ") files changed or added"

say "Building (release). The first build takes a while."
FEATURES=()
if [ "${CRAFT_AI_WHISPER:-0}" = "1" ]; then
  FEATURES=(--features filmcraft/whisper,filmcraft-cli/whisper)
  echo "with speech-to-text (FilmCraft's optional whisper feature)"
fi
# ${arr[@]+...}: an empty array is an "unbound variable" under `set -u` in macOS's bash 3.2
( cd "$DEST" && cargo build --release -p filmcraft -p filmcraft-cli ${FEATURES[@]+"${FEATURES[@]}"} )

app="$DEST/target/release/filmcraft"
cli="$DEST/target/release/filmcraft-cli"
[ -x "$app" ] && [ -x "$cli" ] || die "build finished but the binaries are missing"

say "Done"
echo "App: $app"
echo "CLI: $cli"
if [ "$DEST" != "$HERE/vendor/filmcraft" ]; then
  echo "craft-agent looks in ./vendor/filmcraft by default; for this folder set:"
  echo "  export CRAFT_AI_FILMCRAFT_BIN=\"$DEST/target/release\""
fi
echo "Next: ./craft-agent/craft-agent panel filmcraft --project ~/Movies/my-edit   then Window > AI"
