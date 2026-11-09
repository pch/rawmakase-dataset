"""HDR reference storage tests; no RAWs, Photoshop or live database."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

import dataset
import hdr


class StoreArrayTests(unittest.TestCase):
    def test_a_photo_is_stored_as_float16_within_its_bound(self):
        rng = np.random.default_rng(1)
        linear = rng.uniform(1e-4, 40.0, (6, 7, 3))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'a.npz'
            record = hdr.store_array(linear, path, photo=True)
            with np.load(path) as data:
                back = data['linear']
            self.assertEqual(back.dtype, np.float16)
            np.testing.assert_array_equal(back, linear.astype(np.float16))
            self.assertLessEqual(record['max_luminance_error_stops'], 0.001)
            self.assertEqual(record['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertFalse((Path(folder) / 'a.tmp.npz').exists())

    def test_a_synthetic_job_is_stored_exactly(self):
        linear = np.array([[[0.1234567890123, 15.84, 49.26]]])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'b.npz'
            record = hdr.store_array(linear, path, photo=False)
            with np.load(path) as data:
                np.testing.assert_array_equal(data['linear'], linear)
            self.assertEqual(record['dtype'], 'float64')

    def test_values_beyond_float16_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                hdr.store_array(np.full((1, 1, 3), 70000.0), Path(folder) / 'c.npz', photo=True)


class ReturnedSettingsTests(unittest.TestCase):
    def test_attribute_and_element_forms(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'r.xmp'
            path.write_text('<x crs:ProcessVersion="15.4"><crs:CameraProfile>Adobe Standard</crs:CameraProfile></x>')
            self.assertEqual(hdr._returned(path, 'ProcessVersion'), '15.4')
            self.assertEqual(hdr._returned(path, 'CameraProfile'), 'Adobe Standard')
            self.assertIsNone(hdr._returned(path, 'Exposure2012'))


class SplitTests(unittest.TestCase):
    def test_the_gate_splits_as_the_dataset_does(self):
        for make, model in (('Nikon', 'Z 8'), ('Sony', 'ILCE-7RM4'), ('Canon', 'EOS 850D')):
            self.assertEqual(hdr.split_of(make, model),
                             dataset.split_of(dict(source='pixls', make=make, model=model, captured=None)))


if __name__ == '__main__':
    unittest.main()
