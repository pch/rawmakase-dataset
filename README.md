# RAWmakase dataset

A classified copy of the whole [raw.pixls.us](https://raw.pixls.us/) archive (about 2,000 samples, 56 GB) and of Signature Edits' free RAW photos (823, private), for choosing test photos for [RAWmakase](https://github.com/pch/rawmakase) by camera, exposure, measured tone and scene, plus frozen Camera Raw reference renders for its scene-tone gate.

A clone of this repository is the dataset folder. It carries the metadata: the database (`data/dataset.sqlite`), the raw.pixls.us listing it was built from (`index/`) and the Camera Raw reference sets' recipes and manifests (`references/`). The RAW files, previews and renders are made locally and never committed. Stored paths are relative to the clone.

**Using it for rendering tests:** [docs/using.md](docs/using.md): choosing photos, running RAWmakase's photo tests on a selection, and scoring RAWmakase against the frozen Camera Raw references.

## Setting it up

```
git clone https://github.com/pch/rawmakase-dataset.git && cd rawmakase-dataset
./setup.sh
```

`setup.sh` clones raw.pixls.us' RAW files into `raws/pixls/` at the commit the database was built from (`index/pixls-commit`; `--latest` takes today's), downloads Signature Edits' RAWs (`--no-signature-edits` skips them), checks every file's SHA-256, copies the database to a working `dataset.sqlite` and makes previews and thumbnails. It needs git, git-lfs, exiftool and Python 3.10+, takes about 76 GB, and resumes where it stopped.

## Building it from scratch

Every step of `scripts/dataset.py` skips what it has done, so runs can be stopped and resumed. Run Python with `-I`: the RAWs are untrusted input. The dataset folder defaults to the repository (`--dataset` or `$RAWMAKASE_DATASET` to use another).

```
py="python3 -I scripts/dataset.py"
$py index && $py verify && $py exif && $py cameras && $py previews
RAWMAKASE_BIN=<a RAWmakase build> $py stats --in-scope
$py describe                          # local vision model (pip install -r requirements-describe.txt), ~10 s a scene on an M1 Pro
$py snapshot                          # the working database -> data/dataset.sqlite, to commit
```

## Contents (2026-10-12)

| Source | Samples | Measured | Described | Rated good |
|---|---|---|---|---|
| raw.pixls.us | 2,016 (1,008 from cameras announced 2016 or later) | 948 | 946 | 791 |
| Signature Edits (private) | 823 (831 files; 31 gallery files rejected: JPEGs named like RAWs, DNGs made from JPEGs or TIFFs) | 823 | 821 | 772 |
| The owner's photos (private, local only) | 105 | 105 | 105 | 103 |

Camera Raw 18.7 references, every one checked against the settings Camera Raw applied and frozen with its hash (13,287 references, 0 skipped):

| Folder | Photos | Tone (train / validation / holdout) | Color (train / validation / holdout) |
|---|---|---|---|
| `camera-raw-18.7` (raw.pixls.us, CC0) | 175 | 1,073 / 618 / 654 | 627 / 333 / 349 |
| `camera-raw-18.7-signature-edits` | 239 | 1,470 / 845 / 845 | 924 / 521 / 514 |
| `camera-raw-18.7-own` (git-ignored) | 99 | 1,379 / 725 / 789 | 756 / 414 / 451 |

The renders take about 250 GB: 67.6 GB for raw.pixls.us, 98.9 GB for Signature Edits and 83.1 GB for the owner's photos.

## Sources

| Source | Folder | Terms | Published? |
|---|---|---|---|
| [raw.pixls.us](https://raw.pixls.us/) | `raws/pixls/` (Git LFS clone) | Per sample: CC0 or CC BY-NC-SA | CC0 samples may be |
| [Signature Edits free RAW photos](https://www.signatureedits.com/free-raw-photos/) | `raws/signature-edits/` | [Free Raw Files license](https://www.signatureedits.com/free-raw-license-terms/): free to use, not to redistribute or compile into a similar service; no rights to recognizable people's likeness | **Never**: local tests only |
| The dataset owner's photos | `raws/own/` (placed by hand) | Private | **Never**, and kept out of the committed database snapshot |

Signature Edits adds what raw.pixls.us lacks, people above all. `dataset.py signature-edits` lists the four galleries on their page through the page's own gallery plugin and downloads each RAW through its Download action, one at a time with a pause; `index/signature-edits.json` records every file's Drive id, original name, file name in `raws/signature-edits/` (the original name; a name two files share gets the Drive id appended), size and SHA-256, so a redownload is checked against it. The plugin's request format may change, in which case the command needs updating. The `sources` table records each source's terms and whether it is publishable; a public release must leave out every sample, measurement and description of an unpublishable source.

Files of `rejected` entries in `index/signature-edits.json` (JPEGs in the galleries named like RAWs) are not downloaded again. `dataset.py index-folder` indexes the owner's photos from `raws/own/`; `snapshot` writes no row of that source, so only the machine that has the photos knows about them. The camera-age scope (`in_scope`: announced in 2016 or later) narrows raw.pixls.us only; every Signature Edits and own photo is in scope.

## What is in the database

Samples are keyed by SHA-256, which is raw.pixls.us' checksum and the LFS object id.

- `samples`: every file with its source, make, model, raw mode, license and path inside its source's folder.
- `cameras`: each model's announcement month and category from `data/camera-years.json`; `in_scope` is announced in 2016 or later (`cameras --since`).
- `exif`: lens, focal length, aperture, shutter, ISO, exposure compensation, flash, white balance, size and capture time (exiftool's full JSON is in `meta/exif/`, which holds GPS positions and serial numbers and is not for publishing).
- `stats`: the raw data's scene luminance with the camera's white balance, relative to the white level: percentiles in EV, clipped and black shares, dark and bright areas, a 1 EV histogram, a 48-across map of log2 luminance, and whether RAWmakase opens the file.
- `descriptions`: scene, light, tonal key and contrast, tone features (backlit, light sources, sky, haze, small dark areas in bright surroundings, large dark areas, skin, texture, gradients) and a 0–2 usefulness score per tone control, as [docs/describe.md](docs/describe.md) defines them: scene fields from a local vision model looking at the camera's preview, tone fields from the raw numbers.

The `photos` view joins them. `manifest` turns a query into a manifest in RAWmakase's `tests/corpus/pixls.json` format and, with `--link`, symlinks the files into `$RAWMAKASE_CORPUS/raws/pixls`, where RAWmakase's photo tests read them:

```
$py manifest --where "in_scope AND license = 'CC0-1.0' AND parity_use = 'good' AND t_shadows = 2" --out shadows.json --link
```

## Camera Raw reference sets

Frozen Camera Raw renders of chosen photos for RAWmakase's scene-tone gate (`scripts/corpus/gate.py` in RAWmakase, from pch/rawmakase#416; until it merges, `--gate` points at a checkout of its branch). The renders stay in `references/camera-raw-<version>/` locally and are never committed or re-rendered; their recipe, pool and set manifests are committed. A Camera Raw update starts a new folder.

```
$py references --gate <RAWmakase checkout with scripts/corpus/gate.py>   # needs Photoshop 2026, idle
```

It runs the whole chain, resuming where it stopped: the pool (good, CC0, one file per scene, opened by RAWmakase; frozen in `candidates.txt` on first use), `gate.py survey` (Camera Raw default renders and brightness strata), `gate-sets` (frozen in `sets/` on first use), and `gate.py reference` for each set, validation and holdout first. `recipe.json` records the Camera Raw version, both repositories' commits, the pool query and hashes of the pool and the sets. `--stage` runs one step.

The Camera Raw 18.7 sets hold 2,345 references of 143 photos (1,073 train, 618 validation, 654 holdout, about 68 GB of renders with the color sets); each set's `references.json` here lists its cases with the hash of every reference and sidecar, so a local copy can be checked.

`gate-sets` writes one manifest per tone control (Contrast, Highlights, Shadows, Whites, Blacks, Texture, Clarity, Dehaze, with that control's six gate settings), `exposure` (±1, ±2 EV) and `mixed` (the gate's combinations, mask and Adobe Color cases), each in a train, validation and holdout split. Photos the classification scores as strong tests of the control come first; each split takes 7, 4 and 4 photos per brightness stratum, interleaving camera makes. A camera model's split is fixed by a hash of its name, so shots of one scene never straddle splits. The owner's photos come from three cameras, so they split by a hash of the capture day instead, which keeps a shoot together and puts both Sony and Fujifilm photos in every split. Photos Camera Raw has no Adobe Standard profile for are left out. The setting names mirror `gate.py`'s `settings()`: when the gate renames a setting, this script follows.

## Credits

- **[raw.pixls.us](https://raw.pixls.us/)**, the raw sample archive run by the [PIXLS.US](https://pixls.us/) community, and its many contributors, who donated the samples. raw.pixls.us lists each sample's contributor and license; CC BY-NC-SA samples must be credited as it states.
- **[Signature Edits](https://www.signatureedits.com/free-raw-photos/)** for its free RAW photos, used under its [Free Raw Files license](https://www.signatureedits.com/free-raw-license-terms/).

This repository adds classifications and measurements, not photos.

Built with [ExifTool](https://exiftool.org/), [LibRaw](https://www.libraw.org/) through [rawpy](https://github.com/letmaik/rawpy), and Qwen3-VL run locally with [mlx-vlm](https://github.com/Blaizzy/mlx-vlm).

## Color reference sets

`gate-sets` also writes color families, which `references` renders itself (there is no color gate yet) into the same `references.json` format, checked against the settings Camera Raw applied: `color-wb` (Daylight, Tungsten, Fluorescent, 3200/5000/8000 K, Tint ±30; mixed and artificial light first), `color-vibrance` (Vibrance ±50/±100, Saturation ±50; skin first), `color-mixer-skin` (orange hue, saturation, luminance ±50; photos with skin), `color-mixer-sky` (blue; photos with sky), `color-mixer-foliage` (green hue and luminance, yellow saturation; foliage in the description) and `color-grading` (shadow, highlight and midtone wheels, Blending, Balance ±50). Setting names are Camera Raw's, as RAWmakase's chart cases use them; each manifest carries its settings under `crs`.

`references --source signature-edits` and `--source own` build the same sets from those sources, in `camera-raw-<version>-signature-edits/` (committed like raw.pixls.us') and `camera-raw-<version>-own/` (git-ignored entirely). Manifests are frozen one by one, so new families are added beside frozen ones.

## Reclassifying

`classify.sh` runs every step for whatever the database has no results for yet (verify, exif, cameras, previews, stats, describe); it needs `RAWMAKASE_BIN` and `requirements-describe.txt` installed in `.venv` (or `PYTHON` set to an interpreter that has it), and resumes where it stopped.

## Licenses

The scripts are MIT licensed (see [LICENSE](LICENSE)). raw.pixls.us samples are CC0 or CC BY-NC-SA, per sample; use CC0 samples for anything published. The license of published dataset files is not decided yet, and nothing derived from private sources (the owner's photos) is ever published.
