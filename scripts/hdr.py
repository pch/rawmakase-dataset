#!/usr/bin/env python3
"""Camera Raw HDR references: the frozen family and the broad gate set.

Both live in DATASET/baselines/hdr/ (git-ignored, local); their indexes are committed
in references/camera-raw-18.7-hdr/. Captures come from RAWmakase's
scripts/corpus/hdr_reference.py (the family) and hdr_gate.py (the gate set).

  freeze FAMILY...   copy captured families (folders `hdr_reference.py capture` wrote)
                     into baselines/hdr/family: one entry per distinct job (source,
                     profile and requested sidecar), the reduced linear Display P3
                     array stored as float16 (photos) or float64 (synthetic), both
                     compressed, checked against the original, plus the requested and
                     returned XMP and the capture record. Append-only: jobs already
                     frozen are never rewritten. Synthetic sources and look files are
                     copied in by hash; photos must be CC0 raw.pixls.us samples of the
                     dataset and are referenced by their dataset path.
  verify             re-hash every frozen file against the family's manifest and the
                     gate set's references
  select             choose the gate set: CC0 raw.pixls.us photos from cameras in scope
                     (announced 2016 or later) that the SDR survey saw Camera Raw render
                     with Adobe Standard, whose raw data reaches near the sensor's clip,
                     spread over makes and models; writes gate/selection.json
  index              write the committed indexes (no pixels, no sidecars, no paths
                     outside the dataset)
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np

CAMERA_RAW = '18.7'
INDEX = Path('references') / f'camera-raw-{CAMERA_RAW}-hdr'
# Display P3's D65 luminance coefficients, as RAWmakase's hdr_reference.py uses.
P3_LUMA = np.array([0.2289745641, 0.6917385218, 0.0792869141])
# Arrays of photos are stored as float16. Its relative step is 2^-11 (0.0007 stop)
# for normal values; below 6.1e-5 of graphics white it is subnormal and coarser.
# Measured on the family's photos (2026-11-09): the stage 1 HDR metrics of a float16
# reference move by at most 0.0006 stop and 0.006 ΔE_ITP against the float64 one.
FLOAT16_FLOOR = 1e-4


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def dataset_root(args):
    root = Path(args.dataset or os.environ.get('RAWMAKASE_DATASET') or Path(__file__).resolve().parents[1])
    if not (root / 'dataset.sqlite').is_file():
        sys.exit(f'{root} has no dataset.sqlite: is the dataset SSD mounted?')
    return root


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=1) + '\n')
    temporary.replace(path)


def store_array(linear, path, photo):
    """Writes `linear` (float64) compressed; returns its storage record. A photo's is
    float16, checked to read back as the float16 rounding and to stay within the
    float16 bound above FLOAT16_FLOOR; a synthetic one's is float64, exactly."""
    stored = linear.astype(np.float16) if photo else linear
    if photo and not np.isfinite(stored).all():
        raise ValueError('A value exceeds float16')
    temporary = path.with_name(path.stem + '.tmp.npz')
    np.savez_compressed(temporary, linear=stored)
    with np.load(temporary) as data:
        back = data['linear']
    if back.dtype != stored.dtype or not np.array_equal(back, stored):
        raise RuntimeError(f'{path.name} does not read back exactly')
    record = dict(dtype=str(stored.dtype), format='npz (zip deflate), key "linear"')
    if photo:
        y = linear @ P3_LUMA
        y16 = back.astype(np.float64) @ P3_LUMA
        bright = y > FLOAT16_FLOOR
        stops = np.abs(np.log2(y16[bright] / y[bright])) if bright.any() else np.zeros(1)
        record.update(max_luminance_error_stops=float(stops.max()),
                      max_abs_error=float(np.abs(back - linear).max()), floor=FLOAT16_FLOOR)
        if stops.max() > 0.001:
            raise RuntimeError(f'{path.name}: float16 error {stops.max():.5f} stop')
    temporary.replace(path)
    record['sha256'] = sha256(path)
    return record


def load_family(folder):
    """A captured family: its manifest records with each job's folder."""
    manifest = json.loads((folder / 'manifest.json').read_text())
    return [(folder / f'{i:03d}', record) for i, record in enumerate(manifest)]


def freeze(args):
    root = dataset_root(args)
    out = root / 'baselines' / 'hdr' / 'family'
    for sub in ('arrays', 'sources', 'looks'):
        (out / sub).mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    keys = {r['key']: i for i, r in enumerate(manifest)}
    db = sqlite3.connect(root / 'dataset.sqlite')
    added = duplicates = 0
    hashed = {}
    for family in args.families:
        family = Path(family).resolve()
        for folder, record in load_family(family):
            job = dict(record['job'])
            key = '/'.join((record['source_sha256'], record['profile_sha256'] or 'embedded',
                            record['requested_xmp_sha256']))
            origin = str(family.relative_to(family.parents[2])) + f'/{folder.name}'
            if key in keys:
                entry = manifest[keys[key]]
                if entry['pixels_sha256'] != record['pixels_sha256']:
                    raise RuntimeError(f'{origin}: same job as {entry["id"]}, different pixels')
                if origin not in entry['origins']:
                    entry['origins'].append(origin)
                    tags = entry.setdefault('tags', [])
                    if job.get('tag') and job['tag'] not in tags:
                        tags.append(job['tag'])
                duplicates += 1
                continue
            for name in ('requested.xmp', 'returned.xmp', 'linear-p3.npy'):
                if sha256(folder / name) != record[{'requested.xmp': 'requested_xmp_sha256',
                                                     'returned.xmp': 'returned_xmp_sha256',
                                                     'linear-p3.npy': 'linear_sha256'}[name]]:
                    raise RuntimeError(f'{folder / name} does not match its capture record')
            source = Path(job['source'])
            photo = not job['licence'].startswith('synthetic')
            if photo:
                row = db.execute("SELECT source, license, path FROM samples WHERE sha256 = ?",
                                 (record['source_sha256'],)).fetchone()
                if not row or row[0] != 'pixls' or row[1] != 'CC0-1.0':
                    raise RuntimeError(f'{origin}: source is not a CC0 raw.pixls.us sample of the dataset')
                path = Path('raws/pixls') / row[2]
                if path not in hashed:
                    hashed[path] = sha256(root / path) if (root / path).is_file() else None
                if hashed[path] != record['source_sha256']:
                    raise RuntimeError(f'{origin}: {path} is missing or changed')
                job['source'] = str(path)
                job['licence'] = 'CC0-1.0, raw.pixls.us'
            else:
                if sha256(source) != record['source_sha256']:
                    raise RuntimeError(f'{origin}: synthetic source changed since its capture')
                copy = out / 'sources' / f'{record["source_sha256"]}{source.suffix.lower()}'
                if not copy.exists():
                    shutil.copyfile(source, copy)
                job['source'] = str(copy.relative_to(root))
            if job.get('look'):
                look = Path(job['look']['file'])
                digest = sha256(look)
                copy = out / 'looks' / f'{digest}.xmp'
                if not copy.exists():
                    shutil.copyfile(look, copy)
                job['look'] = dict(job['look'], file=str(copy.relative_to(root)), name=look.name, sha256=digest)
            array = out / 'arrays' / f'{record["linear_sha256"][:24]}.npz'
            if array.exists():
                storage = next(r['storage'] for r in manifest if r['stored_array'] == str(array.relative_to(out)))
            else:
                storage = store_array(np.load(folder / 'linear-p3.npy'), array, photo)
            index = len(manifest)
            target = out / f'{index:03d}'
            target.mkdir(exist_ok=False)
            for name in ('requested.xmp', 'returned.xmp'):
                shutil.copyfile(folder / name, target / name)
            shutil.copyfile(folder / 'manifest.json', target / 'capture.json')
            entry = {k: v for k, v in record.items() if k not in ('metadata', 'job')}
            entry.update(id=f'{index:03d}', key=key, job=job, tags=[job['tag']] if job.get('tag') else [],
                         origins=[origin], stored_array=str(array.relative_to(out)), storage=storage,
                         process_version=_returned(target / 'returned.xmp', 'ProcessVersion'),
                         returned_profile=_returned(target / 'returned.xmp', 'CameraProfile'),
                         photoshop_output_profile=record['metadata'].get('ICC_Profile:ProfileDescription'))
            manifest.append(entry)
            keys[key] = index
            added += 1
            if added % 50 == 0:
                write_json(manifest_path, manifest)
                print(f'{added} added', flush=True)
    filled = 0
    for entry in manifest:
        for field, name in (('process_version', 'ProcessVersion'), ('returned_profile', 'CameraProfile')):
            if entry.get(field) is None:
                entry[field] = _returned(out / entry['id'] / 'returned.xmp', name)
                filled += entry[field] is not None
    write_json(manifest_path, manifest)
    print(f'{added} jobs added, {duplicates} duplicates folded in, {len(manifest)} in the family'
          + (f'; {filled} returned settings read' if filled else ''))


def _returned(path, name):
    """A Camera Raw setting of a returned sidecar, written as an attribute or an element."""
    import re
    text = path.read_text(errors='replace')
    match = re.search(rf'crs:{name}="([^"]*)"', text) or re.search(rf'<crs:{name}>([^<]*)</crs:{name}>', text)
    return match.group(1) if match else None


def verify(args):
    root = dataset_root(args)
    base = root / 'baselines' / 'hdr'
    bad = checked = 0
    family = base / 'family'
    if (family / 'manifest.json').exists():
        for entry in json.loads((family / 'manifest.json').read_text()):
            folder = family / entry['id']
            pairs = [(folder / 'requested.xmp', entry['requested_xmp_sha256']),
                     (folder / 'returned.xmp', entry['returned_xmp_sha256']),
                     (family / entry['stored_array'], entry['storage']['sha256']),
                     (root / entry['job']['source'], entry['source_sha256'])]
            for path, digest in pairs:
                checked += 1
                if not path.is_file() or sha256(path) != digest:
                    bad += 1
                    print(f'{entry["id"]}: {path.relative_to(root)} missing or changed')
    gate = base / 'gate' / 'references.json'
    if gate.exists():
        for entry in json.loads(gate.read_text()):
            checked += 1
            path = gate.parent / entry['blocks']
            if not path.is_file() or sha256(path) != entry['blocks_sha256']:
                bad += 1
                print(f'gate {entry["sha256"][:10]}: blocks missing or changed')
    print(f'{checked} files checked, {bad} bad')
    return 1 if bad else 0


# Raw tone of photos whose highlights render above graphics white at the HDR defaults
# (Adobe Standard, HDR limit 4). Measured on the family's twelve photos: those with
# 99th-percentile raw values at least 1.3 stops below clip, or a 99.9th percentile
# at least 0.7 below it, have 0.8% or more of their area above white.
SELECT = """
SELECT p.sha256, p.make, p.model, p.year, p.path, p.width, p.height, p.clipped, p.ev_p99, p.ev_p999,
       p.bright_area, p.key, p.scene, p.light, s.cfa
FROM photos p JOIN stats s USING (sha256)
WHERE p.source = 'pixls' AND p.license = 'CC0-1.0' AND p.in_scope = 1
  AND p.raw_ok = 1 AND p.rawmakase_ok = 1
  AND (p.ev_p99 >= -1.3 OR p.ev_p999 >= -0.7)
  AND p.clipped < 0.15
  AND COALESCE(p.scene, '') NOT IN ('junk', 'test-chart')
"""


def split_of(make, model):
    """The dataset's split by camera model (dataset.py's split_of)."""
    n = int(hashlib.sha256(f'{make}/{model}'.encode()).hexdigest()[:8], 16) % 4
    return 'train' if n < 2 else 'validation' if n == 2 else 'holdout'


def select(args):
    root = dataset_root(args)
    db = sqlite3.connect(root / 'dataset.sqlite')
    db.row_factory = sqlite3.Row
    family = root / 'baselines' / 'hdr' / 'family' / 'manifest.json'
    excluded = {r['source_sha256'] for r in json.loads(family.read_text())} if family.exists() else set()
    # Only photos Camera Raw renders with Adobe Standard, as the survey of the SDR
    # references recorded: for other cameras it falls back to a DNG's embedded
    # profile or a maker's own, which the gate does not compare.
    import subprocess
    renders = root / 'references' / f'camera-raw-{CAMERA_RAW}' / 'survey' / 'renders'
    adobe = {Path(x['SourceFile']).stem for x in json.loads(subprocess.run(
        ['exiftool', '-q', '-j', '-XMP-crs:CameraProfile', str(renders)], capture_output=True, text=True,
        check=True).stdout or '[]') if x.get('CameraProfile') == 'Adobe Standard'}
    if not adobe:
        sys.exit(f'No survey renders with Adobe Standard in {renders}')
    rows = [dict(r, path=str(Path('raws/pixls') / r['path'])) for r in db.execute(SELECT + ' ORDER BY p.sha256')
            if r['sha256'] not in excluded and r['sha256'][:16] in adobe]
    # One per model first, then a second per validation or holdout model (which
    # judge a change), round-robin over makes, so no make dominates.
    by_model = {}
    for r in rows:
        by_model.setdefault((r['make'], r['model']), []).append(r)
    for photos in by_model.values():
        # Prefer the photo with the most bright area that is not mostly clipped.
        photos.sort(key=lambda r: (-(r['bright_area'] or 0) + 4 * (r['clipped'] or 0), r['sha256']))
    chosen, rank = [], 0
    while len(chosen) < args.count:
        makes = {}
        for (make, model), photos in sorted(by_model.items()):
            limit = args.per_model_judged if split_of(make, model) != 'train' else args.per_model
            if rank < len(photos) and rank < limit:
                makes.setdefault(make, []).append(photos[rank])
        if not makes:
            break
        layer = []
        depth = max(len(v) for v in makes.values())
        for i in range(depth):
            for make in sorted(makes):
                if i < len(makes[make]):
                    layer.append(makes[make][i])
        chosen += layer[:args.count - len(chosen)]
        rank += 1
    for r in chosen:
        r['split'] = split_of(r['make'], r['model'])
    out = root / 'baselines' / 'hdr' / 'gate'
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / 'selection.json', chosen)
    count = lambda key: {k: sum(1 for r in chosen if r[key] == k) for k in sorted({r[key] for r in chosen})}
    print(f'{len(rows)} candidates, {len(by_model)} models; chose {len(chosen)} from '
          f'{len({(r["make"], r["model"]) for r in chosen})} models')
    print('splits', count('split'))
    print('makes', count('make'))


def index(args):
    root = dataset_root(args)
    base = root / 'baselines' / 'hdr'
    target = root / INDEX
    target.mkdir(parents=True, exist_ok=True)
    family = json.loads((base / 'family' / 'manifest.json').read_text())
    keep = ('id', 'source_sha256', 'profile_sha256', 'camera_raw', 'process_version', 'requested_xmp_sha256',
            'returned_xmp_sha256', 'output_sha256', 'pixels_sha256', 'output_icc_sha256', 'linear_sha256',
            'output_dimensions', 'reduced_dimensions', 'resampling', 'output_transfer', 'reference_white_nits',
            'max_reference_white', 'photoshop_output_profile', 'returned_profile', 'tags', 'storage')
    rows = []
    for entry in family:
        job = entry['job']
        row = {k: entry.get(k) for k in keep}
        photo = not job['licence'].startswith('synthetic')
        row.update(source=job['source'] if photo else 'synthetic', licence=job['licence'], split=job['split'],
                   profile=job['profile'] if job['profile'] == 'embedded' else Path(job['profile']).name,
                   settings=job['settings'], curves=job.get('curves'),
                   look=dict(name=job['look']['name'], amount=job['look'].get('amount', 1),
                             sha256=job['look']['sha256']) if job.get('look') else None)
        rows.append(row)
    write_json(target / 'family.json', rows)
    print(f'family.json: {len(rows)} jobs')
    gate = base / 'gate' / 'references.json'
    if gate.exists():
        refs = json.loads(gate.read_text())
        keep = ('sha256', 'make', 'model', 'split', 'path', 'profile_sha256', 'profile', 'settings', 'camera_raw',
                'process_version', 'requested_xmp_sha256', 'returned_xmp_sha256', 'output_transfer',
                'output_icc_sha256', 'output_dimensions', 'blocks_sha256', 'reduction', 'peak', 'above_white_share')
        rows = [{k: r.get(k) for k in keep} for r in refs]
        for row in rows:
            row['profile'] = Path(row['profile']).name if row.get('profile') else None
        write_json(target / 'gate.json', rows)
        print(f'gate.json: {len(rows)} references')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', help='the dataset folder (default $RAWMAKASE_DATASET, else this clone)')
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze')
    f.add_argument('families', nargs='+')
    sub.add_parser('verify')
    s = sub.add_parser('select')
    s.add_argument('--count', type=int, default=200)
    s.add_argument('--per-model', type=int, default=1, help='photos per training camera model')
    s.add_argument('--per-model-judged', type=int, default=2,
                   help='photos per validation or holdout camera model, which judge a change')
    sub.add_parser('index')
    args = parser.parse_args()
    sys.exit({'freeze': freeze, 'verify': verify, 'select': select, 'index': index}[args.command](args) or 0)


if __name__ == '__main__':
    main()
