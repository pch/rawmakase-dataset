#!/bin/bash
# Sets up this dataset on a new machine: clones raw.pixls.us' RAW files at the
# commit the database was built from (index/pixls-commit), checks every file
# against its hash, and makes the previews and thumbnails. The metadata
# (data/dataset.sqlite, index/, references/) comes with this repository.
#
#   ./setup.sh                    everything; safe to rerun, each step resumes
#   ./setup.sh --latest           use raw.pixls.us' current files; newer samples
#                                 are added without measurements or descriptions
#   ./setup.sh --no-signature-edits   skip the private Signature Edits source
#
# Signature Edits' free RAWs (about 20 GB) are a private source: their license
# allows using them, not redistributing them, so they stay local.
#
# Needs git, git-lfs, exiftool and Python 3.10+. The RAWs take about 76 GB.
set -euo pipefail
cd "$(dirname "$0")"
latest=0 signature_edits=1
for arg in "$@"; do
  case "$arg" in
    --latest) latest=1 ;;
    --no-signature-edits) signature_edits=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

for tool in git git-lfs exiftool python3; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
py() { .venv/bin/python -I scripts/dataset.py --dataset "$PWD" "$@"; }

if [ ! -d raws/pixls/.git ]; then
  echo "== cloning raw.pixls.us (file contents follow)"
  mkdir -p raws
  GIT_LFS_SKIP_SMUDGE=1 git clone https://raw.pixls.us/data.lfs.git raws/pixls
fi
if [ "$latest" = 1 ]; then
  git -C raws/pixls pull --ff-only
else
  git -C raws/pixls fetch -q origin
  git -C raws/pixls -c advice.detachedHead=false checkout -q "$(cat index/pixls-commit)"
fi
echo "== downloading RAW files (resumable)"
git -C raws/pixls lfs pull

# The working database starts as the committed snapshot; it then holds this
# machine's state (verification, previews) and stays out of git.
[ -f dataset.sqlite ] || cp data/dataset.sqlite dataset.sqlite

echo "== matching files to the database"
if [ "$latest" = 1 ]; then py index; else py index --offline; fi
if [ "$signature_edits" = 1 ]; then
  echo "== downloading Signature Edits' RAWs (resumable, one at a time)"
  py signature-edits
fi
echo "== checking every file's SHA-256"
py verify
echo "== previews and thumbnails"
py previews

cat <<'DONE'
Done. Optional steps, for samples the database has no numbers or descriptions for:
  RAWMAKASE_BIN=<a RAWmakase build> .venv/bin/python -I scripts/dataset.py stats --in-scope
  .venv/bin/pip install -r requirements-describe.txt   # Apple silicon
  .venv/bin/python -I scripts/dataset.py describe
DONE
