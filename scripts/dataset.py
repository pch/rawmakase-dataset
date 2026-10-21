#!/usr/bin/env python3
"""A classified copy of the whole raw.pixls.us archive, for choosing test photos.

The archive (about 2,000 samples, 56 GB) is a Git LFS clone kept outside the
repository; this script describes it in one SQLite database next to it, so tone
and parity tests can pick photos by camera, exposure, measured tone and scene:

  DATASET/pixls/            git clone https://raw.pixls.us/data.lfs.git (then git lfs pull)
  DATASET/dataset.sqlite    samples, cameras, exif, stats, descriptions; view `photos`
  DATASET/index/            raw.pixls.us repository listings, one per `index` run
  DATASET/meta/exif/        exiftool's full JSON per sample
  DATASET/meta/previews/    the largest embedded preview, upright
  DATASET/meta/thumbs/      the same at 1024 px, for describing
  DATASET/describe/small/   the 768 px images the vision model looks at

Every sample is keyed by its SHA-256, which is raw.pixls.us' checksum and the
Git LFS object id, so file names and the case of directory names don't matter.

  index            read the repository listing and the clone into `samples`
  verify           hash every checked-out file against the listing
  cameras          load data/camera-years.json into `cameras` (announcement
                   dates; in scope = announced in --since or later)
  exif             exiftool metadata into `exif`
  previews         extract previews and thumbnails (rawpy renders when a RAW has none)
  stats            tone statistics from the raw data (rawpy/LibRaw) and whether
                   RAWmakase opens the file, into `stats`
  describe         describe in-scope photos with a local vision model (mlx-vlm on
                   Apple silicon) and derive their tone fields from `stats`; the
                   fields are defined in docs/describe.md
  gate-sets        manifests of photos per tone control and split for the scene-tone
                   gate, from its survey
  references       Camera Raw reference sets for the gate, replayable: pool, survey,
                   gate-sets and gate.py reference, with a recipe.json (needs
                   Photoshop 2026 and a RAWmakase checkout with scripts/corpus/gate.py)
  signature-edits  download Signature Edits' free RAWs (a private source) into
                   external/signature-edits/ and index them as samples
  index-folder     index a source whose RAWs are simply placed in its folder (the
                   owner's photos, raws/own/; issue reports, raws/reports/)
  snapshot         write the working database to data/dataset.sqlite for committing
  manifest         write a manifest in RAWmakase's tests/corpus/pixls.json format from a query, and
                   optionally link the files into $RAWMAKASE_CORPUS/raws/pixls

Needs exiftool, numpy, Pillow and rawpy, and mlx-vlm for `describe`. Run with -I:
the RAWs are untrusted.

  python3 -I scripts/dataset.py --dataset /path/to/dataset index
"""
import argparse
import collections
import datetime
import hashlib
import importlib.util
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YEARS = ROOT / 'data/camera-years.json'
REPOSITORY = 'https://raw.pixls.us/json/getrepository.php?set=all'
CATEGORIES = {'interchangeable', 'compact', 'medium-format', 'phone', 'drone', 'action', 'cinema',
              'converter', 'other'}
THUMB_EDGE = 1024
# The brightness map stored per photo: block means of log2 luminance, this many
# blocks across the long edge.
GRID_ACROSS = 48
# Luminance below 2^-14 of the white level counts as black.
FLOOR_EV = -14.0
HIST_EDGES = [FLOOR_EV + i for i in range(15)]  # -14 ... 0 EV, 1 EV bins

# Where each source's RAWs live in the dataset folder, and its terms. Only
# publishable sources may appear in a public release; private ones are for local
# tests and references.
SOURCES = {
    'pixls': {'folder': 'raws/pixls', 'name': 'raw.pixls.us', 'url': 'https://raw.pixls.us/',
              'license': 'per sample (CC0-1.0 or CC-BY-NC-SA-4.0)', 'publishable': 1, 'snapshot': 1},
    'signature-edits': {'folder': 'raws/signature-edits', 'name': 'Signature Edits free RAW photos',
                        'url': 'https://www.signatureedits.com/free-raw-photos/',
                        'license': 'Signature Edits Free Raw Files license '
                                   '(https://www.signatureedits.com/free-raw-license-terms/)',
                        'publishable': 0, 'snapshot': 1},
    # The owner's photos: never published, and kept out of the committed snapshot
    # too, since nobody else has the files.
    'own': {'folder': 'raws/own', 'name': "The dataset owner's photos", 'url': None,
            'license': 'private', 'publishable': 0, 'snapshot': 0},
    # Files users attached to RAWmakase issues, in a folder per issue: theirs,
    # for reproducing what they reported; never published and kept out of the
    # committed snapshot, like the owner's photos.
    'reports': {'folder': 'raws/reports', 'name': 'Files attached to RAWmakase issues',
                'url': 'https://github.com/pch/rawmakase/issues', 'license': 'private',
                'publishable': 0, 'snapshot': 0},
}
RAW_EXTENSIONS = {'arw', 'cr2', 'cr3', 'dng', 'nef', 'nrw', 'raf', 'rw2', 'orf', 'ori', 'pef', 'rwl', 'srw', 'x3f',
                  'iiq', '3fr', 'fff', 'gpr'}
SE_LICENSE = 'SignatureEdits-FreeRaw'
SE_LISTING = 'index/signature-edits.json'

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  source TEXT PRIMARY KEY, name TEXT, url TEXT, license TEXT, folder TEXT, publishable INTEGER);
CREATE TABLE IF NOT EXISTS samples (
  sha256 TEXT PRIMARY KEY, pixls_id INTEGER, make TEXT, model TEXT, mode TEXT, license TEXT,
  uploaded TEXT, size_bytes INTEGER, url TEXT, path TEXT, ext TEXT, verified TEXT,
  source TEXT NOT NULL DEFAULT 'pixls');
CREATE TABLE IF NOT EXISTS cameras (
  make TEXT, model TEXT, announced TEXT, year INTEGER, category TEXT, in_scope INTEGER,
  confidence TEXT, note TEXT, earliest_capture TEXT, PRIMARY KEY (make, model));
CREATE TABLE IF NOT EXISTS exif (
  sha256 TEXT PRIMARY KEY, exif_make TEXT, exif_model TEXT, lens TEXT, focal_mm REAL,
  focal35_mm REAL, fnumber REAL, exposure_s REAL, iso REAL, exposure_comp REAL,
  exposure_program TEXT, metering TEXT, flash TEXT, white_balance TEXT, orientation INTEGER,
  width INTEGER, height INTEGER, bits INTEGER, compression TEXT, captured TEXT, has_gps INTEGER);
CREATE TABLE IF NOT EXISTS previews (
  sha256 TEXT PRIMARY KEY, source TEXT, width INTEGER, height INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS stats (
  sha256 TEXT PRIMARY KEY, ok INTEGER, error TEXT, cfa TEXT, white_level REAL,
  clipped REAL, black REAL, ev_p01 REAL, ev_p05 REAL, ev_p25 REAL, ev_p50 REAL, ev_p75 REAL,
  ev_p95 REAL, ev_p99 REAL, ev_p999 REAL, spread_ev REAL, dark_area REAL, bright_area REAL,
  hist TEXT, grid_w INTEGER, grid_h INTEGER, grid BLOB, rawmakase_ok INTEGER,
  rawmakase_error TEXT);
CREATE TABLE IF NOT EXISTS descriptions (
  sha256 TEXT PRIMARY KEY, description TEXT, described_by TEXT, described TEXT);
DROP VIEW IF EXISTS photos;
CREATE VIEW photos AS
SELECT s.sha256, s.source, s.pixls_id, s.make, s.model, s.mode, s.license, s.path, s.url, s.size_bytes,
  c.year, c.category,
  -- The camera-age scope narrows raw.pixls.us; curated sources are in scope whole.
  CASE WHEN s.source = 'pixls' THEN c.in_scope ELSE 1 END AS in_scope,
  e.lens, e.focal35_mm, e.fnumber, e.exposure_s, e.iso, e.exposure_comp, e.flash,
  e.white_balance, e.width, e.height, e.captured,
  t.ok AS raw_ok, t.clipped, t.black, t.ev_p01, t.ev_p50, t.ev_p99, t.ev_p999, t.spread_ev,
  t.dark_area, t.bright_area, t.rawmakase_ok,
  json_extract(d.description, '$.subject') AS subject,
  json_extract(d.description, '$.scene') AS scene,
  json_extract(d.description, '$.setting') AS setting,
  json_extract(d.description, '$.light') AS light,
  json_extract(d.description, '$.key') AS key,
  json_extract(d.description, '$.contrast') AS contrast,
  json_extract(d.description, '$.backlit') AS backlit,
  json_extract(d.description, '$.light_sources') AS light_sources,
  json_extract(d.description, '$.sky') AS sky,
  json_extract(d.description, '$.haze') AS haze,
  json_extract(d.description, '$.small_dark_in_bright') AS small_dark_in_bright,
  json_extract(d.description, '$.large_dark_areas') AS large_dark_areas,
  json_extract(d.description, '$.skin') AS skin,
  json_extract(d.description, '$.texture') AS texture,
  json_extract(d.description, '$.smooth_gradients') AS smooth_gradients,
  json_extract(d.description, '$.saturated_colors') AS saturated_colors,
  json_extract(d.description, '$.tone_tests.exposure') AS t_exposure,
  json_extract(d.description, '$.tone_tests.highlights') AS t_highlights,
  json_extract(d.description, '$.tone_tests.shadows') AS t_shadows,
  json_extract(d.description, '$.tone_tests.whites') AS t_whites,
  json_extract(d.description, '$.tone_tests.blacks') AS t_blacks,
  json_extract(d.description, '$.tone_tests.contrast') AS t_contrast,
  json_extract(d.description, '$.tone_tests.clarity') AS t_clarity,
  json_extract(d.description, '$.tone_tests.texture') AS t_texture,
  json_extract(d.description, '$.tone_tests.dehaze') AS t_dehaze,
  json_extract(d.description, '$.quality_flags') AS quality_flags,
  json_extract(d.description, '$.parity_use') AS parity_use,
  json_extract(d.description, '$.tone_notes') AS tone_notes
FROM samples s
LEFT JOIN cameras c ON c.make = s.make AND c.model = s.model
LEFT JOIN exif e USING (sha256)
LEFT JOIN stats t USING (sha256)
LEFT JOIN descriptions d USING (sha256);
"""

# The scene description each photo gets; docs/describe.md explains every field.
DESCRIPTION = {
    'scene': {'landscape', 'cityscape', 'architecture', 'interior', 'portrait', 'people', 'street',
              'night', 'closeup', 'animal', 'still-life', 'product', 'vehicle', 'sky', 'test-chart',
              'color-checker', 'junk'},
    'setting': {'indoor', 'outdoor', 'mixed', 'unknown'},
    'light': {'direct-sun', 'overcast', 'open-shade', 'golden-hour', 'blue-hour', 'night-artificial',
              'tungsten', 'fluorescent-led', 'mixed', 'flash', 'studio', 'unknown'},
    'key': {'low', 'mid', 'high'},
    'contrast': {'low', 'normal', 'high'},
    'sky': {'none', 'small', 'large'},
    'haze': {'none', 'light', 'strong'},
    'texture': {'none', 'some', 'lots'},
    'parity_use': {'good', 'limited', 'useless'},
}
FLAGS = ['backlit', 'light_sources', 'small_dark_in_bright', 'large_dark_areas', 'skin',
         'smooth_gradients', 'saturated_colors']
TONE_TESTS = ['exposure', 'highlights', 'shadows', 'whites', 'blacks', 'contrast', 'clarity', 'texture',
              'dehaze']
QUALITY_FLAGS = {'blurry', 'motion-blur', 'black-frame', 'lens-cap', 'overexposed', 'underexposed',
                 'heavy-noise', 'tiny-preview', 'partial-frame', 'heavy-vignetting'}


def connect(dataset):
    db = sqlite3.connect(dataset / 'dataset.sqlite')
    # Databases from before there were several sources: every sample is pixls.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'samples'").fetchone() and \
            'source' not in [c[1] for c in db.execute('PRAGMA table_info(samples)')]:
        db.execute("ALTER TABLE samples ADD COLUMN source TEXT NOT NULL DEFAULT 'pixls'")
    db.executescript(SCHEMA)
    for name, s in SOURCES.items():
        db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)',
                   (name, s['name'], s['url'], s['license'], s['folder'], s['publishable']))
    db.commit()
    return db


def raw_file(dataset, source, path):
    """A sample's file: its path inside its source's folder."""
    return Path(dataset) / SOURCES[source or 'pixls']['folder'] / path


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def in_parallel(fn, items, jobs):
    with ProcessPoolExecutor(jobs) as pool:
        yield from pool.map(fn, items, chunksize=4)


# index / verify


def listing(dataset, offline):
    folder = dataset / 'index'
    folder.mkdir(exist_ok=True)
    if offline:
        path = max(folder.glob('repository-*.json'))
    else:
        path = folder / f'repository-{datetime.date.today().isoformat()}.json'
        req = urllib.request.Request(REPOSITORY, headers={'User-Agent': 'rawmakase-pixls-dataset'})
        with urllib.request.urlopen(req, timeout=120) as r:
            path.write_bytes(r.read())
    return json.loads(path.read_text())['data'], path


def lfs_files(clone):
    out = subprocess.run(['git', '-C', str(clone), 'lfs', 'ls-files', '--long'], capture_output=True,
                         text=True, check=True).stdout
    files = {}
    for line in out.splitlines():
        oid, _, path = line.split(' ', 2)
        files[oid] = path
    return files


def index(args):
    rows, path = listing(args.dataset, args.offline)
    clone = args.dataset / SOURCES['pixls']['folder']
    files = lfs_files(clone)
    # raw.pixls.us spells some makes in two cases (Olympus and OLYMPUS); use the
    # commoner spelling so a camera is one row.
    spellings = collections.Counter(r[0] for r in rows)
    canonical = {}
    for make, n in spellings.most_common():
        canonical.setdefault(make.lower(), make)
    db = connect(args.dataset)
    seen = set()
    unparsed = 0
    for r in rows:
        m = re.search(r"href='https://raw\.pixls\.us/getfile\.php/(\d+)/nice/([^']+)'.*?"
                      r"sha256 Checksum'>([0-9a-f]{64})</span>", r[7])
        if not m:
            print(f'  unparsed row: {r[0]} {r[1]} {r[2]}', file=sys.stderr)
            unparsed += 1
            continue
        pid, name, sha = int(m.group(1)), html.unescape(m.group(2)), m.group(3)
        url = f'https://raw.pixls.us/getfile.php/{pid}/nice/{name}'
        lic = 'CC0-1.0' if 'zero' in r[5] else 'CC-BY-NC-SA-4.0' if 'by-nc-sa' in r[5] else \
            re.sub('<[^>]+>', '', r[5])
        local = files.get(sha)
        size = (clone / local).stat().st_size if local and (clone / local).exists() else None
        db.execute('INSERT INTO samples (sha256, pixls_id, make, model, mode, license, uploaded, size_bytes,'
                   ' url, path, ext) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (sha256) DO UPDATE SET'
                   ' pixls_id=excluded.pixls_id, make=excluded.make, model=excluded.model,'
                   ' mode=excluded.mode, license=excluded.license, uploaded=excluded.uploaded,'
                   ' size_bytes=excluded.size_bytes, url=excluded.url, path=excluded.path,'
                   ' ext=excluded.ext, verified = CASE WHEN samples.size_bytes IS excluded.size_bytes'
                   ' AND samples.path IS excluded.path THEN samples.verified END',
                   (sha, pid, canonical[r[0].lower()], r[1], r[2], lic, r[6], size, url, local,
                    Path(name).suffix.lower().lstrip('.')))
        seen.add(sha)
    # Samples raw.pixls.us no longer lists go, with everything derived from them;
    # not when rows failed to parse, which would look the same.
    stale = [] if unparsed else [s for (s,) in db.execute("SELECT sha256 FROM samples WHERE source = 'pixls'")
                                 if s not in seen]
    if unparsed:
        print(f'  {unparsed} rows unparsed: kept samples that seem to be gone', file=sys.stderr)
    for table in ('samples', 'exif', 'previews', 'stats', 'descriptions'):
        db.executemany(f'DELETE FROM {table} WHERE sha256 = ?', [(s,) for s in stale])
    db.commit()
    missing = [s for s in seen if s not in files]
    extra = [p for s, p in files.items() if s not in seen]
    print(f'{len(seen)} samples from {path.name}; {len(stale)} no longer listed, {len(missing)} not in '
          f'the clone, {len(extra)} clone files not listed')
    for p in extra[:20]:
        print(f'  not listed: {p}', file=sys.stderr)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def hash_one(item):
    sha, path = item
    try:
        return sha, sha256(path)
    except OSError as e:
        return sha, f'error: {e}'


def verify(args):
    db = connect(args.dataset)
    todo = [(s, raw_file(args.dataset, src, p)) for s, src, p in
            db.execute('SELECT sha256, source, path FROM samples WHERE path IS NOT NULL'
                       + ('' if args.all else ' AND verified IS NULL'))]
    # Listed samples the clone doesn't have fail too.
    absent = [s for (s,) in db.execute('SELECT sha256 FROM samples WHERE path IS NULL')]
    for sha in absent:
        print(f'  not in the clone {sha[:12]}', file=sys.stderr)
    bad = len(absent)
    for sha, got in in_parallel(hash_one, todo, args.jobs):
        if got == sha:
            db.execute('UPDATE samples SET verified=? WHERE sha256=?', (now(), sha))
        else:
            bad += 1
            db.execute('UPDATE samples SET verified=NULL WHERE sha256=?', (sha,))
            print(f'  mismatch {sha[:12]}: {got[:40]}', file=sys.stderr)
    db.commit()
    print(f'{len(todo)} checked, {bad} bad or missing')
    if bad:
        sys.exit(1)


# cameras


def cameras(args):
    years = json.loads(YEARS.read_text())['cameras']
    db = connect(args.dataset)
    known = set(db.execute('SELECT DISTINCT make, model FROM samples'))
    db.execute('DELETE FROM cameras')
    for c in years:
        if c['category'] not in CATEGORIES:
            sys.exit(f'{c["make"]} {c["model"]}: unknown category {c["category"]}')
        year = int(c['announced'][:4]) if c.get('announced') else None
        db.execute('INSERT INTO cameras (make, model, announced, year, category, in_scope, confidence, note)'
                   ' VALUES (?,?,?,?,?,?,?,?)',
                   (c['make'], c['model'], c.get('announced'), year, c['category'],
                    int(year is not None and year >= args.since), c.get('confidence'), c.get('note') or None))
    # A camera can't have taken photos years before it was announced; such rows
    # (or their EXIF clocks) need a look.
    db.execute('UPDATE cameras SET earliest_capture = (SELECT MIN(e.captured) FROM samples s JOIN exif e'
               ' USING (sha256) WHERE s.make = cameras.make AND s.model = cameras.model'
               " AND e.captured >= '1990')")
    db.commit()
    unknown = sorted((m, n) for m, n in known - {(c['make'], c['model']) for c in years} if m and n)
    for make, model in unknown:
        print(f'  no year: {make} {model}', file=sys.stderr)
    for make, model, year, first in db.execute(
            'SELECT make, model, year, earliest_capture FROM cameras WHERE year IS NOT NULL'
            ' AND CAST(substr(earliest_capture, 1, 4) AS INTEGER) < year - 1'):
        print(f'  check: {make} {model} announced {year}, photo from {first[:10]}', file=sys.stderr)
    n = db.execute('SELECT COUNT(*), SUM(in_scope) FROM cameras').fetchone()
    print(f'{n[0]} cameras, {n[1]} announced in {args.since} or later; {len(unknown)} without a year')


# exif


def exif_date(v):
    m = re.match(r'(\d{4}):(\d\d):(\d\d) (\d\d):(\d\d):(\d\d)', str(v or ''))
    return f'{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}' if m and m[1] != '0000' else None


def first(d, *keys):
    for k in keys:
        for g, v in d.items():
            if g.split(':')[-1] == k and v not in (None, '', 'n/a', 'Unknown'):
                return v
    return None


def number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_exiftool(item):
    sha, path, out = item
    p = subprocess.run(['exiftool', '-j', '-G1', '-a', '-n', '-struct', '-api', 'LargeFileSupport=1', path],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.startswith('['):
        return sha, None, p.stderr.strip()[:200]
    Path(out).write_text(p.stdout)
    return sha, json.loads(p.stdout)[0], None


def exif(args):
    db = connect(args.dataset)
    folder = args.dataset / 'meta/exif'
    folder.mkdir(parents=True, exist_ok=True)
    done = {r[0] for r in db.execute('SELECT sha256 FROM exif')}
    todo = [(s, str(raw_file(args.dataset, src, p)), str(folder / f'{s}.json'))
            for s, src, p in db.execute('SELECT sha256, source, path FROM samples WHERE path IS NOT NULL')
            if args.redo or s not in done]
    failed = 0
    for sha, d, err in in_parallel(run_exiftool, todo, args.jobs):
        if d is None:
            failed += 1
            print(f'  exiftool failed {sha[:12]}: {err}', file=sys.stderr)
            continue
        db.execute('INSERT OR REPLACE INTO exif VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            sha, first(d, 'Make'), first(d, 'Model', 'UniqueCameraModel'),
            first(d, 'LensModel', 'LensID', 'LensType', 'Lens'),
            number(first(d, 'FocalLength')), number(first(d, 'FocalLengthIn35mmFormat', 'FocalLength35efl')),
            number(first(d, 'FNumber', 'Aperture')), number(first(d, 'ExposureTime', 'ShutterSpeed')),
            number(first(d, 'ISO', 'RecommendedExposureIndex', 'BaseISO')),
            number(first(d, 'ExposureCompensation', 'ExposureBiasValue')),
            str(first(d, 'ExposureProgram') or '') or None, str(first(d, 'MeteringMode') or '') or None,
            str(first(d, 'Flash') or '') or None, str(first(d, 'WhiteBalance') or '') or None,
            int(number(first(d, 'Orientation')) or 1),
            int(number(first(d, 'ImageWidth', 'RawImageWidth', 'ExifImageWidth')) or 0) or None,
            int(number(first(d, 'ImageHeight', 'RawImageHeight', 'ExifImageHeight')) or 0) or None,
            int(number(str(first(d, 'BitsPerSample') or '').split(' ')[0]) or 0) or None,
            str(first(d, 'Compression') or '') or None,
            exif_date(first(d, 'DateTimeOriginal', 'CreateDate', 'ModifyDate')),
            int(first(d, 'GPSLatitude', 'GPSPosition') is not None)))
    db.commit()
    print(f'{len(todo) - failed} read, {failed} failed')


# previews


PREVIEW_TAGS = ['JpgFromRaw', 'PreviewImage', 'OtherImage', 'PreviewJXL', 'ThumbnailImage']


def make_preview(item):
    """The largest embedded JPEG, turned upright with the RAW's orientation; a
    rawpy half-size render when the RAW holds nothing usable."""
    import io
    from PIL import Image, ImageOps
    sha, path, preview, thumb, orientation = item
    best = None
    for tag in PREVIEW_TAGS:
        data = subprocess.run(['exiftool', '-b', f'-{tag}', path], capture_output=True).stdout
        if data[:2] != b'\xff\xd8':
            continue
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            continue
        if best is None or img.width * img.height > best[1].width * best[1].height:
            best = (tag, img)
    source = best[0] if best else None
    img = best[1] if best else None
    if img is None or max(img.size) < 640:
        try:
            import rawpy
            with rawpy.imread(path) as r:
                rgb = r.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
            img, source = Image.fromarray(rgb), 'rawpy'
            orientation = 1  # LibRaw already turned it
        except Exception as e:
            if img is None:
                return sha, None, None, None, f'no preview: {e}'[:200]
    T = Image.Transpose  # the eight EXIF orientations, as Pillow's exif_transpose maps them
    turn = {2: T.FLIP_LEFT_RIGHT, 3: T.ROTATE_180, 4: T.FLIP_TOP_BOTTOM, 5: T.TRANSPOSE, 6: T.ROTATE_270,
            7: T.TRANSVERSE, 8: T.ROTATE_90}
    if source != 'rawpy' and 0x0112 in img.getexif():
        img = ImageOps.exif_transpose(img)  # the preview says how to turn itself, even "upright"
    elif source != 'rawpy' and orientation in turn:
        img = img.transpose(turn[orientation])
    img = img.convert('RGB')
    img.save(preview, quality=92)
    small = img.copy()
    small.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    small.save(thumb, quality=88)
    return sha, source, img.width, img.height, None


def previews(args):
    db = connect(args.dataset)
    for sub in ('previews', 'thumbs'):
        (args.dataset / 'meta' / sub).mkdir(parents=True, exist_ok=True)
    # Done means the row and its files: a dataset fetched from elsewhere has the
    # rows but not the images, which this rebuilds from the same RAWs.
    done = {r[0] for r in db.execute('SELECT sha256 FROM previews WHERE error IS NULL')
            if (args.dataset / f'meta/thumbs/{r[0]}.jpg').exists()}
    todo = [(s, str(raw_file(args.dataset, src, p)), str(args.dataset / f'meta/previews/{s}.jpg'),
             str(args.dataset / f'meta/thumbs/{s}.jpg'), o or 1)
            for s, src, p, o in db.execute('SELECT sha256, source, path, orientation FROM samples LEFT JOIN exif'
                                           ' USING (sha256) WHERE path IS NOT NULL')
            if args.redo or s not in done]
    if args.redo:
        # Replaced previews may differ, so their descriptions (and the model's
        # copy of the image) go first, committed before any file is replaced.
        for sha, *_ in todo:
            db.execute('DELETE FROM descriptions WHERE sha256 = ?', (sha,))
            (args.dataset / f'describe/small/{sha}.jpg').unlink(missing_ok=True)
        db.commit()
    failed = 0
    for sha, source, w, h, err in in_parallel(make_preview, todo, args.jobs):
        failed += err is not None
        if err:
            print(f'  {sha[:12]}: {err}', file=sys.stderr)
        db.execute('INSERT OR REPLACE INTO previews VALUES (?,?,?,?,?)', (sha, source, w, h, err))
    db.commit()
    print(f'{len(todo) - failed} previews, {failed} failed')


# stats


def tone_stats(path):
    """Scene-linear luminance of the raw data relative to its white level, with
    the camera's as-shot white balance: percentiles in EV (0 = white level), the
    clipped and black fractions of photosites, and a block map of log2 luminance.
    Measured on blocks of whole CFA repeats, so every block has all colors."""
    import numpy as np
    import rawpy
    with rawpy.imread(path) as r:
        try:
            raw = r.raw_image_visible
        except Exception:
            raw = None
        wb = [v for v in r.camera_whitebalance]
        white = float(r.white_level)
        # Monochrome sensors and some reduced modes report color indices
        # outside the 4-entry tables; LibRaw's own output handles them.
        # So do layouts other than RGB (CMYG and the like, on old cameras).
        odd = raw is not None and raw.ndim == 2 and r.raw_pattern is not None and \
            (int(r.raw_colors_visible.max()) >= len(r.black_level_per_channel)
             or not set(r.color_desc.decode()) <= set('RGB'))
        if raw is None or raw.ndim != 2 or r.raw_pattern is None or odd:
            # Linear DNGs and other three-color data: LibRaw's linear output.
            # adjust_maximum_thr=0: keep the file's white level, not this frame's maximum.
            rgb = r.postprocess(gamma=(1, 1), no_auto_bright=True, adjust_maximum_thr=0, output_bps=16,
                                use_camera_wb=True,
                                half_size=True, user_flip=0).astype(np.float32) / 65535
            # Clipping in the camera's own channels before white balance and
            # color conversion, as for CFA data.
            native = r.postprocess(gamma=(1, 1), no_auto_bright=True, adjust_maximum_thr=0, output_bps=16,
                                   user_wb=[1, 1, 1, 1],
                                   output_color=rawpy.ColorSpace.raw, user_flip=0)  # full size: single
            # clipped photosites would average away in half-size output
            clipped = float((native.max(axis=2) >= 0.995 * 65535).mean())
            lum = rgb[..., 0] if rgb.shape[2] == 1 else rgb @ np.array([0.25, 0.5, 0.25], np.float32)
            cfa = 'linear'
        else:
            raw = raw.astype(np.float32)
            colors = r.raw_colors_visible
            blacks = np.array(r.black_level_per_channel, np.float32)
            pattern = np.array(r.raw_pattern)
            desc = r.color_desc.decode()
            # Per-channel white levels when the file has them.
            whites = r.camera_white_level_per_channel
            whites = np.array(whites, np.float32) if whites is not None and min(whites) > 0 else \
                np.full(4, white, np.float32)
            span = whites[colors] - blacks[colors]
            norm = (raw - blacks[colors]) / np.maximum(span, 1)
            clipped = float((norm >= 0.99).mean())
            # Camera WB multipliers by color index (G2 uses G's when missing);
            # LibRaw's daylight multipliers when the file has no as-shot WB.
            if len(wb) < 3 or min(wb[:3]) <= 0:
                wb = [v for v in r.daylight_whitebalance]
            if len(wb) < 3 or min(wb[:3]) <= 0:
                wb = [1.0, 1.0, 1.0, 1.0]
            mult = np.array([wb[i] if i < len(wb) and wb[i] > 0 else wb[1] for i in range(4)], np.float32)
            mult = mult / mult[1]
            # Weights of the color indices the pattern uses: X-Trans says RGBG
            # but only uses 0-2, so its one green index gets green's full weight.
            weight = {'R': 0.25, 'G': 0.5, 'B': 0.25}
            used = set(np.unique(pattern).tolist())
            n_green = sum(1 for i, c in enumerate(desc) if c == 'G' and i in used)
            cw = np.array([(weight.get(c, 0.25) / (max(n_green, 1) if c == 'G' else 1)) if i in used else 0
                           for i, c in enumerate(desc)] + [0] * (4 - len(desc)), np.float32)
            if len(used) == 1:
                cw[list(used)[0]] = 1  # monochrome: the one channel is the luminance
            ph, pw = pattern.shape
            h, w = (norm.shape[0] // ph) * ph, (norm.shape[1] // pw) * pw
            norm, colors = norm[:h, :w], colors[:h, :w]
            v = np.clip(norm, 0, None) * mult[colors]
            # Mean per color within each pattern repeat, then the weighted sum.
            lum = np.zeros((h // ph, w // pw), np.float32)
            for c in range(len(desc)):
                mask = (colors == c).astype(np.float32)
                count = mask.reshape(h // ph, ph, w // pw, pw).sum(axis=(1, 3))
                total = (v * mask).reshape(h // ph, ph, w // pw, pw).sum(axis=(1, 3))
                lum += cw[c] * total / np.maximum(count, 1)
            cfa = 'xtrans' if pattern.shape == (6, 6) else 'bayer' if pattern.shape == (2, 2) else \
                f'{pattern.shape[0]}x{pattern.shape[1]}'
    ev = np.log2(np.maximum(lum, 2.0 ** FLOOR_EV))
    pct = np.percentile(ev, [1, 5, 25, 50, 75, 95, 99, 99.9])
    hist, _ = np.histogram(ev, bins=HIST_EDGES + [math.inf])
    hist = (hist / ev.size).round(5).tolist()
    # The map: block means of log2 luminance, GRID_ACROSS blocks on the long edge.
    b = max(1, max(ev.shape) // GRID_ACROSS)
    gh, gw = ev.shape[0] // b, ev.shape[1] // b
    grid = ev[:gh * b, :gw * b].reshape(gh, b, gw, b).mean(axis=(1, 3)).astype(np.float16)
    median = float(pct[3])
    return dict(cfa=cfa, white_level=white, clipped=clipped, black=float((ev <= FLOOR_EV).mean()),
                pct=[float(p) for p in pct], spread=float(pct[6] - pct[0]),
                # Share of the frame 3 EV under / 2 EV over the median: large
                # shadows and highlights for Shadows and Highlights tests.
                dark=float((ev < median - 3).mean()), bright=float((ev > median + 2).mean()),
                hist=hist, grid=grid)


def measure(item):
    sha, path, rawmakase = item
    row = {'sha': sha}
    try:
        row.update(tone_stats(path))
        row['ok'], row['error'] = 1, None
    except Exception as e:
        row['ok'], row['error'] = 0, f'{type(e).__name__}: {e}'[:300]
    if rawmakase:
        try:
            p = subprocess.run([rawmakase, 'inspect', path], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'RAWMAKASE_DATA_DIR': os.environ.get('TMPDIR', '/tmp')})
            row['rm_ok'] = int(p.returncode == 0)
            row['rm_error'] = None if p.returncode == 0 else \
                (p.stderr.strip().splitlines() or ['?'])[-1][:300]
        except (subprocess.SubprocessError, OSError) as e:
            row['rm_ok'], row['rm_error'] = 0, f'{type(e).__name__}: {e}'[:300]
    else:
        row['rm_ok'] = row['rm_error'] = None
    return row


def stats(args):
    db = connect(args.dataset)
    rawmakase = str(args.rawmakase) if args.rawmakase and args.rawmakase.exists() else None
    if not rawmakase:
        print(f'  {args.rawmakase} not built: skipping the RAWmakase open check', file=sys.stderr)
    db.row_factory = sqlite3.Row
    # Rows measured without the binary still need their open check.
    done = {r[0] for r in db.execute('SELECT sha256 FROM stats'
                                     + (' WHERE rawmakase_ok IS NOT NULL' if rawmakase else ''))}
    where = " AND (c.in_scope = 1 OR s.source != 'pixls')" if args.in_scope else ''
    todo = [(s, str(raw_file(args.dataset, src, p)), rawmakase)
            for s, src, p in db.execute('SELECT s.sha256, s.source, s.path FROM samples s LEFT JOIN cameras c'
                                   ' ON c.make = s.make AND c.model = s.model'
                                   f' WHERE s.path IS NOT NULL{where}')
            if args.redo or s not in done]
    failed = 0
    for i, r in enumerate(in_parallel(measure, todo, args.jobs)):
        failed += not r['ok']
        if not r['ok']:
            print(f'  {r["sha"][:12]}: {r["error"]}', file=sys.stderr)
        p = r.get('pct') or [None] * 8
        g = r.get('grid')
        if r['rm_ok'] is None:
            # No binary this run: keep the earlier open check.
            old = db.execute('SELECT rawmakase_ok, rawmakase_error FROM stats WHERE sha256 = ?',
                             (r['sha'],)).fetchone()
            if old:
                r['rm_ok'], r['rm_error'] = old
        db.execute('INSERT OR REPLACE INTO stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            r['sha'], r['ok'], r['error'], r.get('cfa'), r.get('white_level'), r.get('clipped'),
            r.get('black'), *p, r.get('spread'), r.get('dark'), r.get('bright'),
            json.dumps(r['hist']) if r.get('hist') else None,
            g.shape[1] if g is not None else None, g.shape[0] if g is not None else None,
            g.tobytes() if g is not None else None, r['rm_ok'], r['rm_error']))
        if i % 50 == 49:
            # Descriptions follow their numbers in the same commit.
            rederive(db, quiet=True)
            db.commit()
            print(f'  {i + 1}/{len(todo)}', flush=True)
    rederive(db)  # tone fields come from these numbers; commits the last batch with them
    print(f'{len(todo) - failed} measured, {failed} failed')


# descriptions

VLM = 'mlx-community/Qwen3-VL-8B-Instruct-4bit'
DESCRIBE_EDGE = 768
# What the vision model fills in, from the camera's preview. The tone fields
# (key, contrast, tone_tests, parity_use) come from the raw numbers in derive().
PROMPT = """You label photos for testing a RAW photo editor's tone controls. Look at the photo and fill in every field below.

Fields:
- subject: a concrete one-line description of what is in the photo (e.g. "harbour at dusk with backlit fishing boats")
- scene: one of landscape, cityscape, architecture, interior, portrait, people, street, night, closeup, animal, still-life, product, vehicle, sky, test-chart, color-checker, junk. Use junk only when there is nothing to see at all: a lens cap, a black or blank frame, a blurred ceiling. A plain but real scene (a lawn, a wall with a plant, a view through branches) is not junk; pick the closest real scene.
- setting: one of indoor, outdoor, mixed, unknown
- light: one of direct-sun, overcast, open-shade, golden-hour, blue-hour, night-artificial, tungsten, fluorescent-led, mixed, flash, studio, unknown
- sky: none, small, or large (more than a quarter of the frame)
- haze: none, light, or strong (fog, mist, smoke, atmospheric haze)
- backlit: true if the main subject is darker than what is behind it
- light_sources: true if the sun, lamps, neon or strong specular highlights are in frame
- small_dark_in_bright: true if small dark areas sit inside brighter surroundings (dark windows in a lit wall, a small dark subject in a bright frame)
- large_dark_areas: true if dark regions cover a large part of the frame
- skin: true if people's skin is visible at a useful size
- texture: none, some, or lots (foliage, fabric, rock, bark, hair, tiles)
- smooth_gradients: true if there are large smooth gradients (clear sky, plain walls, backdrop)
- saturated_colors: true if there are strongly saturated areas
- quality_flags: list, any of blurry, motion-blur, black-frame, lens-cap, overexposed, underexposed, heavy-noise, heavy-vignetting; [] if none
- tone_notes: one sentence on where the shadows and highlights are and what detail they hold

Answer with only a JSON object with exactly these keys, in this order:
{"subject": "...", "scene": "...", "setting": "...", "light": "...", "sky": "...", "haze": "...", "backlit": true|false, "light_sources": true|false, "small_dark_in_bright": true|false, "large_dark_areas": true|false, "skin": true|false, "texture": "...", "smooth_gradients": true|false, "saturated_colors": true|false, "quality_flags": [...], "tone_notes": "..."}
Decide each true|false from the photo; do not default to false."""
SEEN = ['subject', 'scene', 'setting', 'light', 'sky', 'haze', 'texture', 'quality_flags', 'tone_notes',
        'backlit', 'light_sources', 'small_dark_in_bright', 'large_dark_areas', 'skin', 'smooth_gradients',
        'saturated_colors']
NOT_SCENES = {'junk', 'test-chart', 'color-checker'}
FLAWS = {'blurry', 'motion-blur', 'heavy-noise', 'overexposed', 'underexposed', 'tiny-preview'}


def check_description(d):
    problems = []
    for k, allowed in DESCRIPTION.items():
        if d.get(k) not in allowed:
            problems.append(f'{k}={d.get(k)!r}')
    for k in FLAGS:
        if not isinstance(d.get(k), bool):
            problems.append(f'{k} not a boolean')
    tt = d.get('tone_tests') or {}
    for k in TONE_TESTS:
        if tt.get(k) not in (0, 1, 2):
            problems.append(f'tone_tests.{k}={tt.get(k)!r}')
    if not isinstance(d.get('quality_flags'), list) or not set(d['quality_flags']) <= QUALITY_FLAGS:
        problems.append(f'quality_flags={d.get("quality_flags")!r}')
    for k in ('subject', 'tone_notes', 'parity_reason'):
        if not isinstance(d.get(k), str) or not d[k].strip():
            problems.append(f'{k} missing')
    return problems


def seen_problems(d):
    """check_description for the model's fields alone, before derive() relies
    on them."""
    tone = ('key', 'contrast', 'parity', 'tone_tests')
    filled = {**d, 'key': 'mid', 'contrast': 'normal', 'parity_use': 'good', 'parity_reason': '-',
              'tone_tests': dict.fromkeys(TONE_TESTS, 0)}
    return [p for p in check_description(filled) if not p.startswith(tone)]


def derive(d, t):
    """The tone fields, from the raw statistics `t` (a stats row as a dict) and
    what the model saw. Thresholds are in EV below the white level; see
    docs/describe.md."""
    real = d['scene'] not in NOT_SCENES
    flags = set(d['quality_flags'])
    p01, p50, p99, p999 = t['ev_p01'], t['ev_p50'], t['ev_p99'], t['ev_p999']
    spread, clipped, black = t['spread_ev'], t['clipped'], t['black']
    d['key'] = 'low' if p50 < -7 else 'high' if p50 > -3 else 'mid'
    d['contrast'] = 'low' if spread < 6 else 'high' if spread > 9 else 'normal'
    if not real:
        tests = dict.fromkeys(TONE_TESTS, 0)
        tests['exposure'] = 1 if d['scene'] != 'junk' else 0
    else:
        tests = {
            'exposure': 2 if clipped < 0.001 and black < 0.01 and 5 <= spread <= 10 else 1,
            # Bright areas with detail close to clipping.
            'highlights': 2 if (p99 > -1.5 or t['bright_area'] > 0.1) and clipped < 0.005 else
            1 if p99 > -3 else 0,
            'whites': 2 if clipped >= 0.0005 or p999 > -0.3 or d['light_sources'] else
            1 if p999 > -1.5 else 0,
            'shadows': 2 if t['dark_area'] > 0.15 or ((d['large_dark_areas'] or d['small_dark_in_bright'])
                                                      and spread > 7) else
            1 if t['dark_area'] > 0.03 or d['backlit'] else 0,
            # Deep tones that still carry detail rather than sitting at black.
            'blacks': 2 if p01 < p50 - 6 and black < 0.02 else 1 if p01 < p50 - 4 else 0,
            'contrast': 2 if 6 <= spread <= 10 and clipped < 0.005 else 1,
            'clarity': 2 if d['texture'] != 'none' and d['scene'] in
            {'landscape', 'cityscape', 'architecture', 'street', 'portrait', 'people', 'interior'} else 1,
            'texture': {'none': 0, 'some': 1, 'lots': 2}[d['texture']],
            'dehaze': {'none': 0, 'light': 1, 'strong': 2}[d['haze']],
        }
    d['tone_tests'] = tests
    if not real and d['scene'] == 'junk' or flags & {'black-frame', 'lens-cap'}:
        d['parity_use'], d['parity_reason'] = 'useless', 'Nothing to look at in the frame.'
    elif not real:
        d['parity_use'], d['parity_reason'] = 'limited', f'A {d["scene"]}, not a scene.'
    elif flags & FLAWS or clipped > 0.05:
        bad = sorted(flags & FLAWS)
        d['parity_use'] = 'limited'
        d['parity_reason'] = ', '.join(bad + ([f'{clipped:.0%} clipped'] if clipped > 0.05 else [])) + '.'
    else:
        d['parity_use'], d['parity_reason'] = 'good', 'A real scene without obvious flaws.'
    return d


def dhash(path):
    """A difference hash and the mean level (0-255): flat frames (black, white)
    all hash to 0, so the mean tells them apart."""
    from PIL import Image
    img = Image.open(path).convert('L').resize((9, 8), Image.Resampling.LANCZOS)
    px = img.tobytes()
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1]), \
        sum(px) / len(px)


def parse_answer(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise ValueError('no JSON object')
    d = json.loads(m.group(0))
    missing = [k for k in SEEN if k not in d]
    if missing:
        raise ValueError(f'missing {missing}')
    d = {k: d[k] for k in SEEN}
    if not isinstance(d['quality_flags'], list):
        raise ValueError('quality_flags is not a list')
    d['quality_flags'] = [f for f in d['quality_flags'] if f in QUALITY_FLAGS]
    return d


def rederive(db, quiet=False):
    """Recompute the tone fields of every description from its stored scene
    fields and the current stats, without the model."""
    # Without measurements there are no tone fields to keep.
    gone = db.execute('DELETE FROM descriptions WHERE sha256 IN (SELECT sha256 FROM stats WHERE ok = 0)').rowcount
    if gone:
        print(f'  {gone} descriptions dropped: their raw could no longer be measured', file=sys.stderr)
    n = 0
    for r in db.execute('SELECT p.*, d.description AS stored, d.described_by FROM photos p'
                        ' JOIN descriptions d USING (sha256) WHERE p.raw_ok = 1').fetchall():
        # Only the derived fields change; anything else stored is kept.
        d = derive(json.loads(r['stored']), dict(r))
        db.execute('UPDATE descriptions SET description = ?, described_by = ? WHERE sha256 = ?',
                   (json.dumps(d, sort_keys=True), r['described_by'].split(';')[0] + '; tone fields v1',
                    r['sha256']))
        n += 1
    db.commit()
    if not quiet:
        print(f'{n} descriptions rederived')


def describe(args):
    """Scene descriptions with a local vision model (mlx-vlm on Apple silicon): no
    API, no tokens. The same scene shot in several raw modes (one camera, previews
    that look alike) is described once."""
    db = connect(args.dataset)
    db.row_factory = sqlite3.Row
    if args.rederive:
        return rederive(db)
    from PIL import Image
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template
    rows = db.execute(
        'SELECT p.*, MAX(v.width, v.height) AS preview_edge FROM photos p JOIN previews v USING (sha256)'
        ' WHERE p.in_scope = 1 AND v.error IS NULL AND p.raw_ok = 1'
        + ('' if args.redo else ' AND p.sha256 NOT IN (SELECT sha256 FROM descriptions)')
        + ' ORDER BY p.make, p.model, p.captured, p.sha256').fetchall()
    if args.limit is not None:
        rows = rows[:args.limit]
    thumbs = args.dataset / 'meta/thumbs'
    # Group look-alikes per camera; the first of a group is described. Shots
    # within a minute of each other (one scene in several raw modes, slightly
    # moved) may differ more.
    def when(r):
        try:
            return datetime.datetime.fromisoformat(r['captured']).timestamp()
        except (TypeError, ValueError):
            return None
    groups = []
    for r in rows:
        (h, level), t = dhash(thumbs / f'{r["sha256"]}.jpg'), when(r)
        for g in groups:
            bits = bin(g['hash'] ^ h).count('1')
            close = t is not None and g['when'] is not None and abs(t - g['when']) <= 60
            if g['camera'] == (r['make'], r['model']) and abs(g['level'] - level) <= 24 and \
                    (bits <= args.same or close and bits <= 3 * args.same):
                g['rows'].append(r)
                break
        else:
            groups.append({'camera': (r['make'], r['model']), 'hash': h, 'level': level, 'when': t,
                           'rows': [r]})
    print(f'{len(rows)} photos, {len(groups)} scenes to describe with {args.model}', flush=True)
    model, processor = load(args.model)
    prompt = apply_chat_template(processor, model.config, PROMPT, num_images=1)
    small = args.dataset / 'describe/small'
    small.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for i, g in enumerate(groups, 1):
        head = g['rows'][0]
        img_path = small / f'{head["sha256"]}.jpg'
        if not img_path.exists():
            img = Image.open(thumbs / f'{head["sha256"]}.jpg')
            img.thumbnail((DESCRIBE_EDGE, DESCRIBE_EDGE), Image.Resampling.LANCZOS)
            img.save(img_path, quality=88)
        seen = None
        for _ in range(2):
            out = generate(model, processor, prompt, [str(img_path)], max_tokens=400, temperature=0.0,
                           verbose=False)
            try:
                seen = parse_answer(getattr(out, 'text', out))
                if not seen_problems(seen):
                    break
                seen = None
            except (ValueError, TypeError, AttributeError):
                seen = None
        if seen is None:
            failed += 1
            print(f'  {head["sha256"][:12]} ({head["make"]} {head["model"]}): no usable answer', file=sys.stderr)
            continue
        for r in g['rows']:
            # Each file keeps its own raw numbers: modes of one scene can differ.
            d = dict(seen, quality_flags=list(seen['quality_flags']))
            if r['preview_edge'] < 640 and 'tiny-preview' not in d['quality_flags']:
                d['quality_flags'].append('tiny-preview')  # too small to judge detail
            d = derive(d, dict(r))
            if r is not head:
                d['same_scene_as'] = head['sha256']
            problems = check_description(d)
            if problems:
                print(f'  {r["sha256"][:12]}: {", ".join(problems)}', file=sys.stderr)
                continue
            db.execute('INSERT OR REPLACE INTO descriptions VALUES (?,?,?,?)',
                       (r['sha256'], json.dumps(d, sort_keys=True), f'{args.model}; tone fields v1', now()))
            done += 1
        db.commit()
        if i % 10 == 0:
            print(f'  {i}/{len(groups)} scenes', flush=True)
    print(f'{done} photos described, {failed} scenes failed')


# gate sets

# Each tone control's Camera Raw settings in the scene-tone gate
# (RAWmakase's scripts/corpus/gate.py, settings()); `default` is always rendered too.
GATE_SETTINGS = {
    control: [f'{control}{v:+d}' for v in (-100, -50, -25, 25, 50, 100)]
    for control in ('contrast', 'highlights', 'shadows', 'whites', 'blacks', 'texture', 'clarity', 'dehaze')}
GATE_SETTINGS['exposure'] = ['exposure-2', 'exposure-1', 'exposure+1', 'exposure+2']
GATE_SETTINGS['mixed'] = ['exposure-1+whites+100', 'shadows+50+highlights-50', 'contrast+50+blacks-50',
                          'dehaze+40+clarity+40', 'mask-exposure+shadows', 'color-default', 'color-shadows+100',
                          'color-dehaze+40', 'color-whites+100']
SPLITS = {'train': 7, 'validation': 4, 'holdout': 4}  # photos per brightness stratum
STRATA = ('low', 'mid', 'high')


def split_of(photo):
    """A photo's split, fixed by a hash so it never moves between runs: half
    train, a quarter validation, a quarter holdout. By camera model, as gate.py
    splits, so a scene shot in several modes (and look-alike shots the scene
    grouping missed) can't be in two splits. The owner's photos come from a few
    cameras, so a split by model would leave whole splits to one camera (or
    none); they split by capture day instead, which keeps a shoot together."""
    key = photo['captured'][:10] if photo['source'] == 'own' and photo['captured'] else \
        f"{photo['make']}/{photo['model']}"
    n = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 4
    return 'train' if n < 2 else 'validation' if n == 2 else 'holdout'


# Color families: Camera Raw settings by name, as RAWmakase's chart cases
# (tests/corpus/cases.json) write them. They have no gate yet, so this script
# renders them itself (render_color) into the same references.json format.
CUSTOM = lambda kelvin, tint: {'WhiteBalance': 'Custom', 'Temperature': str(kelvin), 'Tint': f'{tint:+d}'}
COLOR_SETTINGS = {
    'wb': {'wb-daylight': {'WhiteBalance': 'Daylight'}, 'wb-tungsten': {'WhiteBalance': 'Tungsten'},
           'wb-fluorescent': {'WhiteBalance': 'Fluorescent'}, 'temperature-3200': CUSTOM(3200, 0),
           'temperature-5000': CUSTOM(5000, 0), 'temperature-8000': CUSTOM(8000, 0),
           'tint-30': CUSTOM(6500, -30), 'tint+30': CUSTOM(6500, 30)},
    'vibrance': {'vibrance-100': {'Vibrance': '-100'}, 'vibrance-50': {'Vibrance': '-50'},
                 'vibrance+50': {'Vibrance': '+50'}, 'vibrance+100': {'Vibrance': '+100'},
                 'saturation-50': {'Saturation': '-50'}, 'saturation+50': {'Saturation': '+50'}},
    'mixer-skin': {f'orange-{k}{v:+d}': {f'{a}Orange': f'{v:+d}'} for k, a in
                   (('hue', 'HueAdjustment'), ('sat', 'SaturationAdjustment'), ('lum', 'LuminanceAdjustment'))
                   for v in (-50, 50)},
    'mixer-sky': {f'blue-{k}{v:+d}': {f'{a}Blue': f'{v:+d}'} for k, a in
                  (('hue', 'HueAdjustment'), ('sat', 'SaturationAdjustment'), ('lum', 'LuminanceAdjustment'))
                  for v in (-50, 50)},
    'mixer-foliage': {**{f'green-hue{v:+d}': {'HueAdjustmentGreen': f'{v:+d}'} for v in (-50, 50)},
                      **{f'yellow-sat{v:+d}': {'SaturationAdjustmentYellow': f'{v:+d}'} for v in (-50, 50)},
                      **{f'green-lum{v:+d}': {'LuminanceAdjustmentGreen': f'{v:+d}'} for v in (-50, 50)}},
    'grading': {'grading-shadows-h210': {'SplitToningShadowHue': '210', 'SplitToningShadowSaturation': '50'},
                'grading-highlights-h40': {'SplitToningHighlightHue': '40', 'SplitToningHighlightSaturation': '50'},
                'grading-midtones-h30': {'ColorGradeMidtoneHue': '30', 'ColorGradeMidtoneSat': '50'},
                'grading-blending-100': {'SplitToningShadowHue': '210', 'SplitToningShadowSaturation': '50',
                                         'SplitToningHighlightHue': '40', 'SplitToningHighlightSaturation': '50',
                                         'ColorGradeBlending': '100'},
                'grading-balance-50': {'SplitToningShadowHue': '210', 'SplitToningShadowSaturation': '50',
                                       'SplitToningHighlightHue': '40', 'SplitToningHighlightSaturation': '50',
                                       'SplitToningBalance': '-50'},
                'grading-balance+50': {'SplitToningShadowHue': '210', 'SplitToningShadowSaturation': '50',
                                       'SplitToningHighlightHue': '40', 'SplitToningHighlightSaturation': '50',
                                       'SplitToningBalance': '+50'}},
}
FOLIAGE = re.compile(r'\b(tree|trees|forest|grass|leaf|leaves|foliage|garden|plant|plants|meadow|field|park|'
                     r'bush|hedge|jungle|vineyard|moss)\b', re.I)
ARTIFICIAL = {'tungsten', 'fluorescent-led', 'mixed', 'night-artificial'}


def families():
    """Every gate set family: its settings, which photos may test it and which
    come first (lower rank first)."""
    out = {}
    for control, names in GATE_SETTINGS.items():
        column = f't_{control}'
        out[control] = {'settings': names, 'crs': None,
                        'eligible': (lambda p: True) if control == 'mixed' else (lambda p, c=column: (p[c] or 0) >= 1),
                        'rank': (lambda p: 0) if control == 'mixed' else (lambda p, c=column: -(p[c] or 0)),
                        'score': None if control == 'mixed' else column}
    rules = {
        'wb': (lambda p: True, lambda p: 0 if p['light'] in ARTIFICIAL else 1 if p['light'] not in ('direct-sun',) else 2),
        'vibrance': (lambda p: True, lambda p: -(2 * (p['skin'] or 0) + (p['saturated_colors'] or 0))),
        'mixer-skin': (lambda p: p['skin'] == 1, lambda p: 0),
        'mixer-sky': (lambda p: p['sky'] in ('small', 'large'), lambda p: 0 if p['sky'] == 'large' else 1),
        'mixer-foliage': (lambda p: bool(FOLIAGE.search(f"{p['subject']} {p['tone_notes']}")),
                          lambda p: {'lots': 0, 'some': 1}.get(p['texture'], 2)),
        'grading': (lambda p: True, lambda p: 0),
    }
    for family, crs in COLOR_SETTINGS.items():
        eligible, rank = rules[family]
        out[f'color-{family}'] = {'settings': list(crs), 'crs': crs, 'eligible': eligible, 'rank': rank, 'score': None}
    return out


def gate_sets(args):
    """Manifests of photos for the gates: one set per tone control and per color
    family, in each split, from a survey (Camera Raw default renders and strata)
    and the classifications. Photos that test a family best come first; within a
    stratum, cameras are interleaved by make. A manifest that exists is frozen:
    references are named by their photo's place in it."""
    db = connect(args.dataset)
    db.row_factory = sqlite3.Row
    surveyed = {p['sha256']: p for p in json.loads(args.survey.read_text())['photos']}
    # The gates compare Adobe Standard renders; Camera Raw falls back to a DNG's
    # embedded profile (or a maker's own) for cameras it has no Adobe Standard for.
    renders = args.survey.parent / 'renders'
    profiles = {Path(x['SourceFile']).stem: x.get('CameraProfile') for x in json.loads(subprocess.run(
        ['exiftool', '-q', '-j', '-XMP-crs:CameraProfile', str(renders)], capture_output=True, text=True,
        check=True).stdout or '[]')}
    other = sorted(s for s in surveyed if profiles.get(s[:16]) != 'Adobe Standard')
    if other:
        print(f'  {len(other)} photos left out: Camera Raw has no Adobe Standard for them', file=sys.stderr)
    suite = getattr(args, 'suite', 'general')
    rows = {r['sha256']: r for r in db.execute('SELECT * FROM photos p WHERE ' + selection_where(suite))}
    pool = [dict(surveyed[s], **{k: rows[s][k] for k in rows[s].keys()}) for s in surveyed
            if s in rows and s not in other and rows[s]['path']
            and rows[s]['rawmakase_ok'] == 1]
    args.out.mkdir(parents=True, exist_ok=True)
    plan = {}
    for family, f in families().items():
        if suite == 'stress' and family.startswith('color-'):
            continue
        eligible = [p for p in pool if suite == 'stress' or f['eligible'](p)]
        for split, quota in SPLITS.items():
            name = f'{family}-{split}'
            target = args.out / f'{name}.json'
            if target.exists():
                if json.loads(target.read_text()).get('suite', 'general') != suite:
                    raise ValueError(f'{target}: another suite is frozen here; use a new --out')
                continue
            chosen = []
            for stratum in STRATA:
                group = sorted((p for p in eligible if split_of(p) == split
                                and p['stratum'] == stratum),
                               key=lambda p: (reference_rank(p, family, suite, f), p['sha256']))
                by_make = {}
                for p in group:
                    by_make.setdefault((reference_rank(p, family, suite, f), p['make']), []).append(p)
                order = []
                for level in sorted({k[0] for k in by_make}):
                    makes = {k: v for k, v in by_make.items() if k[0] == level}
                    while any(makes.values()):
                        for k in sorted(makes):
                            if makes[k]:
                                order.append(makes[k].pop(0))
                chosen += order[:quota]
            photos = [{'raw': f"{SOURCES[p['source']]['folder']}/{p['path']}", 'sha256': p['sha256'],
                       'p90_L': p['p90_L'], 'stratum': p['stratum'], 'make': p['make'], 'model': p['model'],
                       'subject': p['subject'], 'score': p[f['score']] if f['score'] else None}
                      for p in chosen]
            doc = {'schema': 1, 'suite': suite, 'set': name, 'settings': f['settings'], 'photos': photos}
            if f['crs']:
                doc['crs'] = f['crs']
            target.write_text(json.dumps(doc, indent=1) + '\n')
            plan[name] = {s: sum(p['stratum'] == s for p in photos) for s in STRATA}
    for name, counts in plan.items():
        print(f'  {name:26s} ' + ' '.join(f'{s} {n:2d}' for s, n in counts.items()))
    print(f'{len(plan)} new sets, {sum(sum(c.values()) for c in plan.values())} photo slots -> {args.out}')


def render_color(gate, manifest, out, version):
    """Camera Raw references of a color set, as gate.py reference makes them for
    tone sets: Photoshop renders every photo at each setting and at defaults
    (Adobe Standard, 16-bit ProPhoto, 2048 px), each render's sidecar is checked
    against what was asked, and the set is frozen in references.json."""
    spec = importlib.util.spec_from_file_location('camera_raw', gate / 'scripts/corpus/camera_raw.py')
    camera_raw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(camera_raw)
    doc = json.loads(manifest.read_text())
    cases = {'default': {}, **doc['crs']}
    jobs, plan = [], []
    for i, photo in enumerate(doc['photos']):
        if sha256(photo['raw']) != photo['sha256']:
            raise ValueError(f'{photo["raw"]}: changed since the manifest was frozen')
        for name, crs in cases.items():
            target = out / 'references' / f'{i}-{name}.tif'
            jobs.append(dict(source=photo['raw'], settings=dict(crs, CameraProfile='Adobe Standard'), out=str(target),
                             edge=2048))
            plan.append(dict(photo=i, name=name, reference=str(target.relative_to(out))))
    (out / 'references').mkdir(parents=True, exist_ok=True)
    for f in camera_raw.render(jobs, work=out / 'work', chunk=20):
        print('  failed:', f, file=sys.stderr)
    frozen, skipped = [], []
    for c in plan:
        target = out / c['reference']
        if not target.exists():
            skipped.append(dict(c, reason='not rendered'))
            continue
        xmp = target.with_suffix('.xmp')
        if not xmp.exists():
            xmp.write_bytes(subprocess.check_output(['exiftool', '-b', '-XMP', str(target)]))
        tags = json.loads(subprocess.check_output(['exiftool', '-j', '-n', '-XMP-crs:all', str(xmp)]))[0]
        # A case Camera Raw didn't render as asked (a preset a camera lacks, a
        # profile fallback) is left out and recorded, not frozen.
        wrong = [] if tags.get('CameraProfile') == 'Adobe Standard' else [f'profile {tags.get("CameraProfile")!r}']
        for k, v in cases[c['name']].items():
            got = tags.get({'Temperature': 'ColorTemperature'}.get(k, k))  # exiftool's name for crs:Temperature
            same = str(got) == v if k == 'WhiteBalance' else got is not None and abs(float(got) - float(v)) < 1e-4
            if not same:
                wrong.append(f'{k} = {got!r}, expected {v}')
        if wrong:
            skipped.append(dict(c, reason='; '.join(wrong)))
            print(f'  {target.name}: {"; ".join(wrong)}', file=sys.stderr)
            continue
        frozen.append(dict(c, reference_sha256=sha256(target), xmp=str(xmp.relative_to(out)), xmp_sha256=sha256(xmp)))
    (out / 'references.json').write_text(json.dumps(dict(
        schema=1, camera_raw=version, set=doc['set'], photos=doc['photos'], cases=frozen, skipped=skipped), indent=1))
    shutil.rmtree(out / 'work', ignore_errors=True)
    print(f'{len(frozen)} references frozen in {out / "references.json"}')


# references

# The photos Camera Raw references are made from: classified as good, CC0, one
# file per scene, opened by RAWmakase, in a format gate.py reads.
REFERENCE_POOL = """
SELECT 'raws/pixls/' || p.path FROM photos p JOIN descriptions d USING (sha256)
WHERE p.source = 'pixls' AND p.in_scope AND p.parity_use = 'good' AND p.license = 'CC0-1.0' AND p.rawmakase_ok = 1
  AND json_extract(d.description, '$.same_scene_as') IS NULL
  AND lower(substr(p.path, -3)) IN ('arw', 'raf', 'nef', 'nrw', 'cr2', 'cr3', 'dng', 'orf', 'rw2', 'pef', 'rwl')
ORDER BY p.sha256"""
# Other sources: every good photo, one file per scene, that RAWmakase opens.
SOURCE_POOL = """
SELECT '{folder}/' || p.path FROM photos p JOIN descriptions d USING (sha256)
WHERE p.source = '{source}' AND p.parity_use = 'good' AND p.rawmakase_ok = 1
  AND json_extract(d.description, '$.same_scene_as') IS NULL
  AND lower(substr(p.path, -3)) IN ('arw', 'raf', 'nef', 'nrw', 'cr2', 'cr3', 'dng', 'orf', 'rw2', 'pef', 'rwl')
ORDER BY p.sha256"""


def selection_where(suite):
    """One selection rule for both the survey pool and per-control manifests.

    General references stay unchanged. Stress references deliberately include
    capture defects; they never select charts, lens caps or unreadable files.
    """
    if suite == 'general':
        return "p.parity_use = 'good' AND p.rawmakase_ok = 1"
    if suite != 'stress':
        raise ValueError(f'Unknown reference suite: {suite}')
    not_scenes = ', '.join("'" + s + "'" for s in sorted(NOT_SCENES))
    return f"""p.raw_ok = 1 AND p.rawmakase_ok = 1
      AND p.scene NOT IN ({not_scenes})
      AND NOT EXISTS (SELECT 1 FROM json_each(p.quality_flags) WHERE value IN ('black-frame', 'lens-cap'))
      AND (p.clipped >= 0.005 OR p.ev_p50 < -7 OR EXISTS
        (SELECT 1 FROM json_each(p.quality_flags) WHERE value IN ('underexposed', 'overexposed', 'heavy-noise')))"""


def pool_query(source, suite):
    query = REFERENCE_POOL if source == 'pixls' else \
        SOURCE_POOL.format(folder=SOURCES[source]['folder'], source=source)
    return query.replace("p.parity_use = 'good'", '(' + selection_where(suite) + ')')


def reference_rank(photo, family, suite, spec):
    if suite == 'general':
        return spec['rank'](photo)
    # Do not reuse the general ranking: it prefers unclipped Highlights tests.
    # Coarse severity groups still interleave camera makes within each group.
    if family in ('shadows', 'blacks'):
        return 0 if photo['ev_p50'] is not None and photo['ev_p50'] < -7 else 1
    return 0 if photo['clipped'] is not None and photo['clipped'] > .05 else 1
CAMERA_RAW_PLIST = Path('/Library/Application Support/Adobe/Plug-Ins/CC/File Formats/'
                        'Camera Raw.plugin/Contents/Info.plist')


def resolve(dataset, path):
    """A path stored relative to the dataset folder (older files hold absolute ones)."""
    path = Path(path)
    return path if path.is_absolute() else (Path(dataset) / path).resolve()


def git_head(path):
    p = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return p.stdout.strip() or None


def references(args):
    """Camera Raw reference sets for the scene-tone gate, from the pool to the
    frozen renders, in one replayable run: the pool query, gate.py survey,
    gate-sets, then gate.py reference for every set (validation and holdout
    first). Every step skips what exists, so an interrupted run resumes. The
    folder's recipe.json records the inputs: Camera Raw version, the gate and
    this script's commits, the pool and the sets."""
    import plistlib
    gate = args.gate.resolve()
    if not (gate / 'scripts/corpus/gate.py').exists():
        sys.exit(f'{gate} has no scripts/corpus/gate.py (the scene-tone gate, pch/rawmakase#416)')
    version = plistlib.loads(CAMERA_RAW_PLIST.read_bytes())['CFBundleShortVersionString'].split()[0] \
        if CAMERA_RAW_PLIST.exists() else None
    if not version:
        sys.exit('Camera Raw not found')
    # One folder per source: raw.pixls.us' is camera-raw-<version>, others add
    # their name (the owner's is git-ignored entirely).
    suffix = '' if args.source == 'pixls' else f'-{args.source}'
    if args.suite == 'stress':
        suffix += '-stress'
    out = (args.out or args.dataset / 'references' / f'camera-raw-{version}{suffix}').resolve()
    query = pool_query(args.source, args.suite)
    out.mkdir(parents=True, exist_ok=True)
    python = [sys.executable, '-I', str(gate / 'scripts/corpus/gate.py')]
    recipe_path = out / 'recipe.json'
    recipe = json.loads(recipe_path.read_text()) if recipe_path.exists() else {}
    if recipe_path.exists() and recipe.get('suite', 'general') != args.suite:
        sys.exit(f'{out} holds another reference suite; use a new --out')
    if recipe.get('camera_raw', version) != version:
        sys.exit(f'{out} holds Camera Raw {recipe["camera_raw"]} references; this is {version}')
    recipe.update(camera_raw=version, gate_commit=git_head(gate), script_commit=git_head(ROOT),
                  source=args.source, suite=args.suite, pool_query=query.strip())
    # 1. The pool, frozen on first use so later dataset changes don't move it.
    pool = out / 'candidates.txt'
    if not pool.exists():
        db = connect(args.dataset)
        paths = [r[0] for r in db.execute(query)]
        pool.write_text(''.join(f'{p}\n' for p in paths))
    # Paths are relative to the dataset folder, so a clone can use the files.
    paths = [str(resolve(args.dataset, p)) for p in pool.read_text().split('\n')[:-1]]
    recipe['pool'] = {'photos': len(paths), 'sha256': hashlib.sha256(pool.read_bytes()).hexdigest()}
    recipe_path.write_text(json.dumps(recipe, indent=1) + '\n')
    if args.stage in ('survey', 'all'):
        # 2. Camera Raw default renders and brightness strata.
        subprocess.run(python + ['survey', '--out', str(out / 'survey'), '--pool', *paths], check=True,
                       cwd=gate)
        # gate.py records absolute paths; keep them relative to the dataset.
        survey = out / 'survey/pool.json'
        doc = json.loads(survey.read_text())
        root = str(args.dataset.resolve()) + '/'
        for p in doc['photos']:
            if p['raw'].startswith(root):
                p['raw'] = p['raw'][len(root):]
        survey.write_text(json.dumps(doc, indent=1) + '\n')
    if args.stage in ('sets', 'all'):
        # 3. The manifests, each frozen once written: references are named by
        # their photo's place in its set, so a set must never change under them.
        # New families (color) are added beside frozen ones.
        gate_sets(argparse.Namespace(dataset=args.dataset, survey=out / 'survey/pool.json', out=out / 'sets',
                                     suite=args.suite))
        recipe['sets'] = {m.stem: hashlib.sha256(m.read_bytes()).hexdigest()
                          for m in sorted((out / 'sets').glob('*.json'))}
        recipe_path.write_text(json.dumps(recipe, indent=1) + '\n')
    if args.stage in ('render', 'all'):
        # 4. The references, checked against the settings Camera Raw applied and
        # frozen with their hashes in <set>/references.json: tone sets by gate.py
        # reference, color sets (no gate yet) by render_color.
        for split in [x for x in ('validation', 'holdout', 'train') if x in args.splits.split(',')]:
            for frozen in sorted((out / 'sets').glob(f'*-{split}.json')):
                if (out / frozen.stem / 'references.json').exists():
                    continue
                doc = json.loads(frozen.read_text())
                settings = doc['settings']
                # A full disk leaves truncated renders and empty files behind:
                # skip a set that may not fit (16-bit 2048 px TIFFs, ~17 MB each,
                # plus room to spare); a rerun renders it once there is space.
                need = len(doc['photos']) * (len(settings) + 1) * 17e6 + 3e9
                if shutil.disk_usage(out).free < need:
                    print(f'!! {frozen.stem} skipped: needs {need / 1e9:.0f} GB free', flush=True)
                    continue
                print(f'== {frozen.stem}', flush=True)
                # gate.py opens the RAWs by path: give it this machine's.
                for p in doc['photos']:
                    p['raw'] = str(resolve(args.dataset, p['raw']))
                manifest = out / 'resolved' / frozen.name
                manifest.parent.mkdir(exist_ok=True)
                manifest.write_text(json.dumps(doc, indent=1) + '\n')
                # One failing set doesn't stop the others; a rerun retries it.
                try:
                    if 'crs' in doc:
                        render_color(gate, manifest, out / frozen.stem, version)
                    else:
                        subprocess.run(python + ['reference', '--manifest', str(manifest), '--out',
                                                 str(out / frozen.stem), '--chunk', '20', '--settings', *settings],
                                       check=True, cwd=gate)
                except Exception as e:
                    print(f'!! {frozen.stem} failed: {type(e).__name__}: {e}'[:400], flush=True)
                    continue
                # The frozen index names RAWs relative to the dataset, as the
                # manifest does, so it can be committed and used from a clone.
                index = out / frozen.stem / 'references.json'
                if index.exists():
                    done = json.loads(index.read_text())
                    done['photos'] = json.loads(frozen.read_text())['photos']
                    index.write_text(json.dumps(done, indent=1) + '\n')


# Signature Edits

SE_PAGE = 'https://www.signatureedits.com/free-raw-photos/'
SE_AJAX = 'https://www.signatureedits.com/wp-admin/admin-ajax.php'
SE_RAW = {'arw', 'cr2', 'cr3', 'dng', 'nef', 'raf', 'rw2', 'orf', 'pef'}
# EXIF makes as the camera table spells them.
MAKES = {'nikon corporation': 'Nikon', 'nikon': 'Nikon', 'sony': 'Sony', 'canon': 'Canon', 'fujifilm': 'Fujifilm',
         'panasonic': 'Panasonic', 'olympus corporation': 'Olympus', 'olympus imaging corp.': 'Olympus',
         'om digital solutions': 'OM Digital Solutions', 'ricoh imaging company, ltd.': 'Ricoh',
         'pentax': 'Pentax', 'leica camera ag': 'Leica', 'apple': 'Apple', 'dji': 'DJI', 'samsung': 'Samsung',
         'google': 'Google', 'hasselblad': 'Hasselblad', 'sigma': 'Sigma'}


def se_opener():
    from http.cookiejar import CookieJar
    o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    o.addheaders = [('User-Agent', 'rawmakase-dataset (local test set)')]
    return o


def se_listing(o, pause):
    """Drive id -> (name, gallery token, account) for every RAW in the page's
    galleries (a Google Drive folder shown by the Use-your-Drive plugin)."""
    import time
    page = o.open(SE_PAGE, timeout=60).read().decode()
    nonce = re.search(r'gallery_nonce":"([0-9a-f]+)"', page).group(1)
    files = {}
    for token, account in re.findall(r"data-token='([0-9a-f]{32})' data-account-id='(\d+)'", page):
        data = urllib.parse.urlencode({
            'action': 'useyourdrive-get-gallery', 'listtoken': token, 'lastFolder': '', 'folderPath': 'bnVsbA==',
            'sort': 'modified:desc', 'page_url': SE_PAGE, '_ajax_nonce': nonce, 'query': ''}).encode()
        d = json.loads(o.open(SE_AJAX, data, timeout=120).read())
        for drive_id, name in re.findall(r"data-id='([^']+)' data-name='([^']+)'", d['html']):
            name = html.unescape(name)
            if name.rsplit('.', 1)[-1].lower() in SE_RAW:
                files.setdefault(drive_id, (name, token, account))
        time.sleep(pause)
    return files


def camera_name(make, model):
    """EXIF make and model as the camera table names them: 'NIKON CORPORATION',
    'NIKON Z 6' -> 'Nikon', 'Z 6'; 'Canon', 'Canon EOS R5' -> 'Canon', 'EOS R5'."""
    make, model = (make or '').strip(), (model or '').strip()
    canonical = MAKES.get(make.lower(), make.title() if make.isupper() else make)
    for prefix in (make, make.split()[0] if make else '', canonical):
        if prefix and model.lower().startswith(prefix.lower() + ' '):
            model = model[len(prefix) + 1:]
            break
    return canonical, model


def index_folder(args):
    """Index a source whose files are simply placed in its folder (the owner's
    photos): every RAW there becomes a sample, with its camera from EXIF; samples
    whose file is gone are dropped with what was derived from them."""
    source = args.source
    folder = args.dataset / SOURCES[source]['folder']
    files = sorted(p for p in folder.rglob('*') if p.is_file() and p.suffix.lower().lstrip('.') in RAW_EXTENSIONS)
    db = connect(args.dataset)
    spelled = {m.lower(): m for (m,) in db.execute("SELECT DISTINCT make FROM samples WHERE source = 'pixls'")}
    tags = {}
    for start in range(0, len(files), 200):
        out = subprocess.run(['exiftool', '-j', '-fast2', '-Make', '-Model', *map(str, files[start:start + 200])],
                             capture_output=True, text=True).stdout
        tags.update({t['SourceFile']: t for t in json.loads(out or '[]')})
    seen = set()
    for path in files:
        digest = sha256(path)
        seen.add(digest)
        t = tags.get(str(path), {})
        make, model = camera_name(t.get('Make'), t.get('Model'))
        make = spelled.get(make.lower(), make)
        db.execute('INSERT INTO samples (sha256, source, make, model, mode, license, uploaded, size_bytes, url,'
                   ' path, ext) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (sha256) DO UPDATE SET'
                   ' source=excluded.source, make=excluded.make, model=excluded.model, license=excluded.license,'
                   ' size_bytes=excluded.size_bytes, path=excluded.path, ext=excluded.ext',
                   (digest, source, make or None, model or None, '', SOURCES[source]['license'],
                    datetime.date.fromtimestamp(path.stat().st_mtime).isoformat(), path.stat().st_size, None,
                    str(path.relative_to(folder)), path.suffix.lower().lstrip('.')))
    gone = [s for (s,) in db.execute('SELECT sha256 FROM samples WHERE source = ?', (source,)) if s not in seen]
    for table in ('samples', 'exif', 'previews', 'stats', 'descriptions'):
        db.executemany(f'DELETE FROM {table} WHERE sha256 = ?', [(s,) for s in gone])
    db.commit()
    print(f'{len(files)} {source} samples indexed, {len(gone)} gone')


def signature_edits(args):
    """Download Signature Edits' free RAWs (a private source: their license allows
    use but not redistribution) and index them as samples. The file list with
    hashes is index/signature-edits.json; a rerun downloads only what is missing
    or differs from it, one file at a time with a pause."""
    import time
    folder = args.dataset / SOURCES['signature-edits']['folder']
    folder.mkdir(parents=True, exist_ok=True)
    record = args.dataset / SE_LISTING
    known = json.loads(record.read_text()) if record.exists() else {}
    if not args.index_only:
        o = se_opener()
        files = se_listing(o, args.pause)
        print(f'{len(files)} RAW files listed; {len(known)} known', flush=True)
        # One folder, original names; a name more than one file has gets the
        # file's Drive id, decided from the whole listing, not download order.
        uses = collections.Counter(Path(n).name.lower() for n, _, _ in files.values())
        done = failed = 0
        for drive_id, (name, token, account) in sorted(files.items()):
            base = Path(name).name
            if uses[base.lower()] > 1:
                base = f'{Path(base).stem}-{drive_id[:8]}{Path(base).suffix}'
            target = folder / base
            entry = known.get(drive_id)
            if entry and ('rejected' in entry or target.exists() and target.stat().st_size == entry['size']):
                continue
            url = f'{SE_AJAX}?' + urllib.parse.urlencode(
                {'action': 'useyourdrive-download', 'id': drive_id, 'account_id': account, 'listtoken': token, 'dl': 1})
            tmp = target.with_name(target.name + '.part')
            try:
                with o.open(url, timeout=600) as r, open(tmp, 'wb') as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                with open(tmp, 'rb') as f:
                    head = f.read(8)
                if head[:2] not in (b'II', b'MM') and head[:4] != b'FUJI' and head[4:8] != b'ftyp':
                    # Some gallery files are JPEGs with a RAW extension: recorded,
                    # so reruns don't download them again.
                    tmp.unlink()
                    known[drive_id] = {'name': name, 'gallery': token, 'rejected': f'not a RAW (starts {head!r})'}
                    record.write_text(json.dumps(known, indent=1, sort_keys=True) + '\n')
                    print(f'  {name}: not a RAW, skipped from now on', file=sys.stderr, flush=True)
                    continue
                digest = sha256(tmp)
                if entry and entry['sha256'] != digest:
                    print(f'  {name}: differs from the recorded file (new hash kept)', file=sys.stderr)
                tmp.rename(target)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                failed += 1
                print(f'  {name}: {e}', file=sys.stderr, flush=True)
                continue
            known[drive_id] = {'name': name, 'file': target.name, 'gallery': token, 'size': target.stat().st_size,
                               'sha256': digest, 'downloaded': datetime.date.today().isoformat()}
            record.write_text(json.dumps(known, indent=1, sort_keys=True) + '\n')
            done += 1
            if done % 25 == 0:
                print(f'  {done} downloaded', flush=True)
            time.sleep(args.pause)
        print(f'{done} downloaded, {failed} failed')
    # Index: one sample per file, camera from its EXIF.
    db = connect(args.dataset)
    # Makes as the dataset already spells them (raw.pixls.us' HUAWEI, not Huawei).
    spelled = {m.lower(): m for (m,) in db.execute("SELECT DISTINCT make FROM samples WHERE source = 'pixls'")}
    entries = [(i, e) for i, e in sorted(known.items()) if 'rejected' not in e and (folder / e['file']).exists()]
    paths = [str(folder / e['file']) for i, e in entries]
    tags = {}
    for start in range(0, len(paths), 200):
        out = subprocess.run(['exiftool', '-j', '-fast2', '-Make', '-Model', '-UniqueCameraModel',
                              *paths[start:start + 200]], capture_output=True, text=True).stdout
        tags.update({t['SourceFile']: t for t in json.loads(out or '[]')})
    # DNGs Lightroom or Photoshop made from a JPEG or TIFF hold a processed image,
    # not sensor data: rejected like the JPEGs, and never downloaded again.
    converted = [(i, e, p) for (i, e), p in zip(entries, paths)
                 if not tags.get(p, {}).get('Make') and str(tags.get(p, {}).get('UniqueCameraModel', '')).upper()
                 in ('JPEG', 'TIFF', '')]
    for drive_id, e, path in converted:
        kind = tags.get(path, {}).get('UniqueCameraModel') or 'an image'
        known[drive_id] = {'name': e['name'], 'gallery': e.get('gallery'), 'rejected': f'DNG converted from {kind}'}
        Path(path).unlink(missing_ok=True)
        db.execute('DELETE FROM samples WHERE sha256 = ?', (e['sha256'],))
    if converted:
        record.write_text(json.dumps(known, indent=1, sort_keys=True) + '\n')
        print(f'  {len(converted)} DNGs converted from JPEG or TIFF rejected', file=sys.stderr)
        skip = {i for i, _, _ in converted}
        entries, paths = zip(*[(x, p) for x, p in zip(entries, paths) if x[0] not in skip]) if len(skip) < len(entries) \
            else ((), ())
    for (drive_id, e), path in zip(entries, paths):
        t = tags.get(path, {})
        make, model = camera_name(t.get('Make'), t.get('Model'))
        make = spelled.get(make.lower(), make)
        db.execute('INSERT INTO samples (sha256, source, make, model, mode, license, uploaded, size_bytes, url,'
                   ' path, ext) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (sha256) DO UPDATE SET'
                   ' source=excluded.source, make=excluded.make, model=excluded.model, license=excluded.license,'
                   ' size_bytes=excluded.size_bytes, url=excluded.url, path=excluded.path, ext=excluded.ext',
                   (e['sha256'], 'signature-edits', make or None, model or None, '', SE_LICENSE, e['downloaded'],
                    e['size'], SE_PAGE, e['file'],
                    Path(e['name']).suffix.lower().lstrip('.')))
    db.commit()
    print(f'{len(entries)} Signature Edits samples indexed')


# manifest


def manifest(args):
    db = connect(args.dataset)
    rows = db.execute(f'SELECT make, model, mode, url, sha256, size_bytes, license, path, source FROM photos'
                      f' WHERE path IS NOT NULL AND ({args.where}) ORDER BY source, make, model, mode').fetchall()
    # raw.pixls.us files are named as pixls.py downloads them (its URL's name).
    name = lambda r: r[3].rsplit('/', 1)[-1] if r[8] == 'pixls' else Path(r[7]).name
    files = [{'make': r[0], 'model': r[1], 'mode': r[2], 'url': r[3], 'sha256': r[4],
              'size_mb': round((r[5] or 0) / 1e6, 2), 'license': r[6], 'source': r[8]} for r in rows]
    private = sorted({r[8] for r in rows if not SOURCES[r[8]]['publishable']})
    if private:
        print(f'  includes private sources ({", ".join(private)}): local use only', file=sys.stderr)
    args.out.write_text(json.dumps({'about': f'Dataset samples where {args.where}; from rawmakase-dataset '
                                             'scripts/dataset.py. Not committed.',
                                    'files': files}, indent=1) + '\n')
    print(f'{len(files)} files -> {args.out}')
    if not args.link:
        return
    corpus = os.environ.get('RAWMAKASE_CORPUS')
    if not corpus:
        sys.exit('Set RAWMAKASE_CORPUS to link the files')
    # The extensions RAWmakase's photo tests (tests/color/private.rs) and
    # camera-raw-photos.py read; other files would be linked but never used.
    readable = {'arw', 'raf', 'nef', 'nrw', 'cr2', 'cr3', 'dng', 'rw2', 'orf', 'ori', 'pef', 'rwl', 'fff', '3fr'}
    wanted = set()
    for f, r in zip(files, rows):
        if name(r).rsplit('.', 1)[-1].lower() not in readable:
            print(f'  not linked, the photo tests skip .{name(r).rsplit(".", 1)[-1]}: {f["make"]} {f["model"]}',
                  file=sys.stderr)
            continue
        # raws/<source>/<make>/<model>/<file>: pixls.py's layout for raw.pixls.us,
        # so its tests and gate.py find the files.
        target = Path(corpus) / 'raws' / r[8] / f['make'] / f['model'] / name(r)
        source = raw_file(args.dataset, r[8], r[7]).resolve()
        if target.exists() and not target.is_symlink() and target.stat().st_size == source.stat().st_size \
                and sha256(target) == f['sha256']:
            continue  # pixls.py already downloaded this very file
        if target.is_symlink() and target.resolve() != source or target.exists() and not target.is_symlink():
            # Another file has this name (one capture in several modes):
            # tell them apart by hash.
            target = target.with_name(f'{target.stem}-{f["sha256"][:8]}{target.suffix}')
        wanted.add(target)
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
    # The tests read every file under raws/, so links of an earlier selection
    # go; downloaded files and other links stay.
    removed = 0
    # Links are told apart by the file itself, not by their path: on a
    # case-insensitive volume one camera may be listed as "EOS REBEL T7i" and
    # "EOS Rebel T7i", and rglob returns the folder's spelling, not ours.
    wanted = {(st.st_dev, st.st_ino) for st in (os.lstat(t) for t in wanted)}
    for src, s in SOURCES.items():
        base, folder = Path(corpus) / 'raws' / src, (args.dataset / s['folder']).resolve()
        for link in base.rglob('*') if base.exists() else []:
            if link.is_symlink() and (link.lstat().st_dev, link.lstat().st_ino) not in wanted \
                    and Path(os.readlink(link)).is_relative_to(folder):
                link.unlink()
                removed += 1
    print(f'linked {len(wanted)} into {corpus}/raws, removed {removed} links of an earlier selection')


def snapshot(args):
    """Write the working database to data/dataset.sqlite for committing, without
    this machine's state (verification times) or sources kept local (the owner's
    photos)."""
    target = ROOT / 'data/dataset.sqlite'
    tmp = target.with_suffix('.tmp')
    tmp.unlink(missing_ok=True)
    db = connect(args.dataset)
    db.execute('VACUUM INTO ?', (str(tmp),))
    db.close()
    out = sqlite3.connect(tmp)
    out.execute('UPDATE samples SET verified = NULL')
    # Sources kept off git (the owner's photos) leave no trace in the snapshot.
    local = [k for k, v in SOURCES.items() if not v['snapshot']]
    marks = ','.join('?' * len(local))
    for table in ('exif', 'previews', 'stats', 'descriptions'):
        out.execute(f'DELETE FROM {table} WHERE sha256 IN (SELECT sha256 FROM samples WHERE source IN ({marks}))', local)
    out.execute(f'DELETE FROM samples WHERE source IN ({marks})', local)
    out.execute(f'DELETE FROM sources WHERE source IN ({marks})', local)
    out.commit()
    out.execute('VACUUM')
    out.close()
    tmp.replace(target)
    print(f'{target} ({target.stat().st_size / 1e6:.1f} MB)')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dataset', type=Path, default=os.environ.get('RAWMAKASE_DATASET') or ROOT,
                   help='the dataset folder (default $RAWMAKASE_DATASET, else this repository)')
    p.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('index')
    s.add_argument('--offline', action='store_true', help='use the newest saved listing')
    s.set_defaults(run=index)
    s = sub.add_parser('verify')
    s.add_argument('--all', action='store_true', help='also files verified before')
    s.set_defaults(run=verify)
    s = sub.add_parser('cameras')
    s.add_argument('--since', type=int, default=2016, help='first in-scope announcement year')
    s.set_defaults(run=cameras)
    for name, fn in (('exif', exif), ('previews', previews)):
        s = sub.add_parser(name)
        s.add_argument('--redo', action='store_true')
        s.set_defaults(run=fn)
    s = sub.add_parser('stats')
    s.add_argument('--redo', action='store_true')
    s.add_argument('--in-scope', action='store_true', help='only in-scope cameras')
    s.add_argument('--rawmakase', type=Path, default=os.environ.get('RAWMAKASE_BIN'),
                   help='a RAWmakase build for the open check (default $RAWMAKASE_BIN)')
    s.set_defaults(run=stats)
    s = sub.add_parser('describe')
    s.add_argument('--model', default=VLM, help='an mlx-vlm model')
    s.add_argument('--limit', type=lambda v: max(0, int(v)), help='describe at most this many photos')
    s.add_argument('--same', type=int, default=6,
                   help='previews of one camera within this many bits of difference hash are one scene')
    s.add_argument('--redo', action='store_true', help='also photos already described')
    s.add_argument('--rederive', action='store_true',
                   help='only recompute the tone fields of existing descriptions')
    s.set_defaults(run=describe)
    s = sub.add_parser('gate-sets')
    s.add_argument('--suite', choices=['general', 'stress'], default='general')
    s.add_argument('--survey', type=Path, required=True, help="gate.py survey's pool.json")
    s.add_argument('--out', type=Path, required=True)
    s.set_defaults(run=gate_sets)
    s = sub.add_parser('references')
    s.add_argument('--suite', choices=['general', 'stress'], default='general',
                   help='stress: separate clipped, underexposed and noisy capture references')
    s.add_argument('--gate', type=Path, required=True,
                   help='a RAWmakase checkout with scripts/corpus/gate.py (the scene-tone gate)')
    s.add_argument('--out', type=Path, help='default $RAWMAKASE_DATASET/references/camera-raw-<version>')
    s.add_argument('--stage', choices=['pool', 'survey', 'sets', 'render', 'all'], default='all')
    s.add_argument('--source', choices=list(SOURCES), default='pixls')
    s.add_argument('--splits', default='validation,holdout,train',
                   help='which splits to render, comma-separated (validation and holdout judge a change)')
    s.set_defaults(run=references)
    sub.add_parser('snapshot').set_defaults(run=snapshot)
    s = sub.add_parser('index-folder')
    s.add_argument('--source', choices=[k for k in SOURCES if k not in ('pixls', 'signature-edits')], default='own')
    s.set_defaults(run=index_folder)
    s = sub.add_parser('signature-edits')
    s.add_argument('--index-only', action='store_true', help='index the files already downloaded')
    s.add_argument('--pause', type=float, default=2.0, help='seconds between requests')
    s.set_defaults(run=signature_edits)
    s = sub.add_parser('manifest')
    s.add_argument('--where', required=True, help="SQL over the photos view, e.g. \"t_shadows = 2 AND "
                                                  "license = 'CC0-1.0'\"")
    s.add_argument('--out', type=Path, required=True)
    s.add_argument('--link', action='store_true',
                   help='symlink the files into $RAWMAKASE_CORPUS/raws/pixls')
    s.set_defaults(run=manifest)
    args = p.parse_args()
    if not args.dataset:
        sys.exit('Pass --dataset or set RAWMAKASE_DATASET')
    args.dataset = Path(args.dataset)
    args.run(args)


if __name__ == '__main__':
    main()
