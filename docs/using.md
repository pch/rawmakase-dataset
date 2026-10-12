# Using the dataset for RAWmakase rendering tests

How to pick photos from this dataset and run RAWmakase's tests and Camera Raw comparisons on them. Setting the dataset up is in the [README](../README.md); this assumes `./setup.sh` has run, so the RAWs are in `raws/` and the working database is `dataset.sqlite`.

Run every command from the dataset folder: stored paths (`raws/pixls/…`) are relative to it. `py` below is `python3 -I scripts/dataset.py`; `$RAWMAKASE` is a RAWmakase checkout.

## What you can test, and with what

| Test | Uses | Needs |
|---|---|---|
| RAWmakase's photo tests (accepted renders, Camera Raw parity of photo cases) | photos linked into `$RAWMAKASE_CORPUS/raws/` by `manifest --link` | a RAWmakase build; Photoshop only to make new Camera Raw references |
| Scene-tone gate (#416): Exposure, Contrast, Highlights, Shadows, Whites, Blacks, Texture, Clarity, Dehaze | frozen tone sets in `references/camera-raw-<version>*/` | a RAWmakase build with `render-batch`, `$RAWMAKASE/scripts/corpus/gate.py` |
| Color comparisons: white balance, Vibrance/Saturation, color mixer, color grading | frozen color sets (`color-*`) in the same folders | the same; scoring works, there is no acceptance rule for color yet |

## 1. Choosing photos

The `photos` view joins everything known about each sample. Useful columns:

- Source and rights: `source` (`pixls`, `signature-edits`, `own`), `license`, `in_scope`.
- Camera: `make`, `model`, `year`, `category`, and `cfa` in `stats` (Bayer, X-Trans, linear DNG).
- Exposure: `iso`, `exposure_s`, `fnumber`, `exposure_comp`, `flash`, `white_balance`.
- Raw tone, measured on the sensor data: `ev_p01` … `ev_p999` (EV below clipping), `spread_ev`, `clipped`, `black`, `dark_area`, `bright_area`. `raw_ok` says LibRaw measured it; `rawmakase_ok` says RAWmakase opens it.
- Scene, from the local vision model: `subject`, `scene`, `setting`, `light`, `key`, `contrast`, `sky`, `haze`, `backlit`, `light_sources`, `small_dark_in_bright`, `large_dark_areas`, `skin`, `texture`, `smooth_gradients`, `saturated_colors`, `quality_flags`, `tone_notes`.
- Usefulness: `t_exposure` … `t_dehaze` (0 = not useful, 2 = strong test of that control) and `parity_use` (`good`, `limited`, `useless`).

[docs/describe.md](describe.md) defines every field and threshold. Examples:

```sql
-- Strong Shadows tests with small dark areas inside bright surroundings
SELECT sha256, make, model, subject FROM photos
WHERE parity_use = 'good' AND t_shadows = 2 AND small_dark_in_bright;

-- Bright tones near clipping without much clipping: the far end of Highlights
SELECT * FROM photos WHERE t_highlights = 2 AND ev_p999 > -0.5 AND clipped < 0.005;

-- People under artificial light, for white balance and Vibrance
SELECT * FROM photos WHERE skin AND light IN ('tungsten', 'mixed', 'night-artificial');

-- One photo per X-Trans camera, CC0 only (publishable results)
SELECT * FROM photos WHERE sha256 IN (SELECT sha256 FROM stats WHERE cfa = 'xtrans')
  AND license = 'CC0-1.0' AND parity_use = 'good' GROUP BY model;
```

`sqlite3 dataset.sqlite` opens it; any SQLite client works.

## 2. Running RAWmakase's photo tests on a selection

```
export RAWMAKASE_CORPUS=<your corpus folder>
py manifest --where "parity_use = 'good' AND t_shadows = 2" --out shadows.json --link
cd $RAWMAKASE && cargo test --release --test color -- --ignored --nocapture
```

`--link` symlinks each chosen RAW into `$RAWMAKASE_CORPUS/raws/<source>/<make>/<model>/`, where the photo tests (`tests/color/private.rs`) and `scripts/corpus/camera-raw-photos.py` read them, and removes links of an earlier selection; downloaded files and other links stay. Formats the photo tests don't read (IIQ, X3F, CRW…) are listed and skipped. See RAWmakase's `tests/corpus/README.md` for the tests themselves: `photos_match_accepted_renders` (regressions against RAWmakase's last accepted renders) and `photos_camera_raw_parity_does_not_regress` (against Camera Raw, after `camera-raw-photos.py` renders the references).

## 3. Scoring RAWmakase against the frozen Camera Raw references

Each set folder (`references/camera-raw-18.7/shadows-validation/` and so on) holds the Camera Raw renders and their sidecars in `references/`, and `references.json` with the photos, the cases and the hash of every file. The renders themselves are local (about 68 GB for raw.pixls.us' sets, 250 GB for all three sources); `references.json` is committed, so a copy can always be checked.

RAWmakase has to render with Adobe Standard, as Camera Raw did, so give it Adobe's profiles first: a profile library outside both repositories (Adobe profiles never go into either), built from the Camera Raw install, which `render` uses through `--data-dir`:

```
lib=~/rawmakase-gate-data            # any folder; RAWmakase reads <lib>/camera-profiles
mkdir -p $lib/camera-profiles
cp "/Library/Application Support/Adobe/CameraRaw/CameraProfiles/Adobe Standard/"*.dcp $lib/camera-profiles/
cp "/Library/Application Support/Adobe/CameraRaw/Settings/Adobe/Profiles/Adobe Raw/"*.xmp $lib/camera-profiles/   # Adobe Color and other looks
```

```
gate=$RAWMAKASE/scripts/corpus/gate.py
set=references/camera-raw-18.7/shadows-validation
python3 $gate render --refs $set --rawmakase $RAWMAKASE/target/release/rawmakase --data-dir $lib --out renders/candidate/shadows-validation
python3 $gate score  --refs $set --renders renders/candidate/shadows-validation --out scores/candidate/shadows-validation.json
python3 $gate accept --candidate scores/candidate/shadows-validation.json --baseline scores/baseline/shadows-validation.json
```

- `render` develops each RAW once with every case's sidecar, the settings Camera Raw itself recorded, so RAWmakase renders exactly what Camera Raw did. Photos whose Adobe Standard profile RAWmakase can't match are reported ("Missing camera profile 'Adobe Standard'") and scored invalid: so far devices whose profiles Adobe names per lens or module (Google Pixel 7 Pro, DJI's Hasselblad L1D-20c). That is a RAWmakase gap, not a dataset one.
- Checked end to end on `exposure-validation` (2026-10-11): 7 of 9 photos rendered and scored, median ΔE00 1.0–1.2 per setting; the Pixel 7 Pro and L1D-20c photos were invalid as above.
- `score` checks every reference against its frozen hash first, then measures median ΔE00 per photo, signed ΔL* per brightness band and clipping disagreement.
- `accept` applies the gate's acceptance rule against a baseline's scores (render and score the baseline build the same way). It knows the tone settings only: color sets can be rendered and scored, not accepted, until a color gate exists.

Judge engine changes on `validation` and `holdout` sets; `train` sets are for diagnosis and fitting. A photo's split is fixed (by camera model; the owner's photos, from only three cameras, by capture day), so a scene never appears on both sides. If a holdout is inspected after failing, treat it as validation from then on (the gate's rule).

### The reference families

| Folder | Photos | In git |
|---|---|---|
| `camera-raw-18.7/` | raw.pixls.us, CC0 | manifests, indexes, recipe |
| `camera-raw-18.7-signature-edits/` | Signature Edits (private) | manifests, indexes, recipe |
| `camera-raw-18.7-own/` | the owner's photos (private) | nothing |

Sets in each: one per tone control (`contrast`, `highlights`, `shadows`, `whites`, `blacks`, `texture`, `clarity`, `dehaze`), `exposure`, `mixed` (the gate's combinations, mask and Adobe Color cases), and the color families (`color-wb`, `color-vibrance`, `color-mixer-skin`, `color-mixer-sky`, `color-mixer-foliage`, `color-grading`), each as `-train`, `-validation` and `-holdout`. A manifest's `settings` lists its cases; color manifests carry the Camera Raw values under `crs`.

Camera Raw references are never re-rendered or re-approved. A new Camera Raw version gets new folders (`references --source …` with the new version), never mixed with the old.

## 4. Rules for results

- Publish only results from CC0 raw.pixls.us samples. Signature Edits' license forbids redistributing its photos; the owner's photos are private. Neither their renders nor per-photo results leave this machine.
- Name photos by `sha256` in reports, not by path.
- Gaps to keep in mind: raw.pixls.us has almost no dark scenes, people or haze; the dark (low) brightness stratum and skin are filled mainly by the private sources, so results there can't be published per photo.

## 5. Regression tests (planned)

RAWmakase issue #420 plans decode fingerprints for every file and render snapshots over the whole dataset, run on a self-hosted runner that has the dataset. This section will describe them when they exist.
