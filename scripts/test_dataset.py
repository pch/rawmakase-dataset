"""Reference selection tests; no RAWs, Photoshop, downloads or live database."""
import sqlite3
import unittest
import json
import plistlib
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import dataset


class StressSelectionTests(unittest.TestCase):
    def eligible(self, photo, suite):
        db = sqlite3.connect(':memory:')
        columns = ', '.join(photo)
        db.execute(f'CREATE TABLE photos ({columns})')
        db.execute(f'INSERT INTO photos VALUES ({", ".join("?" for _ in photo)})', tuple(photo.values()))
        return bool(db.execute('SELECT 1 FROM photos p WHERE ' + dataset.selection_where(suite)).fetchall())

    def photo(self, **changes):
        return dict(dict(parity_use='limited', raw_ok=1, rawmakase_ok=1,
                         clipped=.12, ev_p50=-4., scene='landscape', quality_flags='[]'), **changes)

    def test_clipped_and_underexposed_are_eligible(self):
        for p in (self.photo(), self.photo(clipped=0., ev_p50=-10.),
                  self.photo(clipped=0., quality_flags='["heavy-noise"]')):
            self.assertTrue(self.eligible(p, 'stress'))
            self.assertFalse(self.eligible(p, 'general'))

    def test_junk_and_unreadable_are_never_stress_photos(self):
        for changes in (dict(scene='junk'), dict(rawmakase_ok=0), dict(raw_ok=0),
                        dict(quality_flags='["lens-cap"]'), dict(quality_flags='["black-frame"]')):
            self.assertFalse(self.eligible(self.photo(**changes), 'stress'))

    def test_stress_query_preserves_license_and_scene_deduplication(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE photos (sha256, source, path, in_scope, parity_use, license, '
                   'rawmakase_ok, raw_ok, clipped, ev_p50, scene, quality_flags)')
        db.execute('CREATE TABLE descriptions (sha256, description)')
        for sha, license, desc in [('ok', 'CC0-1.0', '{}'), ('private', 'CC BY-NC-SA', '{}'),
                                    ('duplicate', 'CC0-1.0', '{"same_scene_as":"ok"}')]:
            db.execute('INSERT INTO photos VALUES (?, ?, ?, 1, ?, ?, 1, 1, .12, -4, ?, ?)',
                       (sha, 'pixls', sha + '.dng', 'limited', license, 'landscape', '[]'))
            db.execute('INSERT INTO descriptions VALUES (?, ?)', (sha, desc))
        self.assertEqual(db.execute(dataset.pool_query('pixls', 'stress')).fetchall(), [('raws/pixls/ok.dng',)])
        self.assertEqual(db.execute(dataset.pool_query('pixls', 'general')).fetchall(), [])

    def test_stress_ranking_does_not_prefer_unclipped_highlights(self):
        spec = {'rank': lambda p: -p['t_highlights']}
        mild = self.photo(clipped=.006, t_highlights=2)
        severe = self.photo(clipped=.15, t_highlights=1)
        self.assertLess(dataset.reference_rank(mild, 'highlights', 'general', spec),
                        dataset.reference_rank(severe, 'highlights', 'general', spec))
        self.assertLess(dataset.reference_rank(severe, 'highlights', 'stress', spec),
                        dataset.reference_rank(mild, 'highlights', 'stress', spec))
        self.assertLess(dataset.reference_rank(self.photo(ev_p50=-10), 'shadows', 'stress', spec),
                        dataset.reference_rank(severe, 'shadows', 'stress', spec))

    def test_normal_well_exposed_scene_is_not_stress(self):
        self.assertFalse(self.eligible(self.photo(parity_use='good', clipped=0., ev_p50=-4), 'stress'))

    def test_unknown_suite_is_rejected(self):
        with self.assertRaises(ValueError):
            dataset.selection_where('typo')

    def test_fresh_stress_pool_then_resume_and_refuse_general_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'gate/scripts/corpus/gate.py'
            script.parent.mkdir(parents=True)
            script.touch()
            plist = root / 'camera-raw.plist'
            plist.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '18.7'}))
            args = types.SimpleNamespace(gate=root / 'gate', dataset=root, source='pixls',
                                         suite='stress', out=None, stage='pool')
            with patch.object(dataset, 'CAMERA_RAW_PLIST', plist), \
                 patch.object(dataset, 'git_head', return_value='test'), \
                 patch.object(dataset, 'connect') as connect:
                connect.return_value.execute.return_value = [('raws/pixls/synthetic.dng',)]
                dataset.references(args)
                out = root / 'references/camera-raw-18.7-stress'
                original = (out / 'candidates.txt').read_bytes()
                dataset.references(args)
                self.assertEqual((out / 'candidates.txt').read_bytes(), original)
                self.assertEqual(json.loads((out / 'recipe.json').read_text())['suite'], 'stress')
                args.suite, args.out = 'general', out
                with self.assertRaises(SystemExit):
                    dataset.references(args)
                self.assertEqual((out / 'candidates.txt').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
