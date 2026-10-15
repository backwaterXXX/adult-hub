import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scene_scraper_fingerprint_sources as sources


class _Response:
    status_code = 200

    def __init__(self, scenes):
        self.scenes = scenes

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"findScenesBySceneFingerprints": [self.scenes]}}


class StashDbPhashThresholdTests(unittest.TestCase):
    def _lookup(self, stored_hash, scene_duration=120):
        scene = {
            "id": "scene-1",
            "title": "Scene",
            "duration": scene_duration,
            "fingerprints": [{"algorithm": "PHASH", "hash": stored_hash}],
        }
        response = _Response([scene])
        with patch.dict(os.environ, {"STASHDB_API_KEY": "test-key"}), patch.object(
            sources._core, "request_with_host_limit", return_value=response
        ):
            return sources.search_stashdb_scene_by_fingerprint(
                "0000000000000000",
                "PHASH",
                http_client=object(),
                max_distance=10,
                duration=120,
                duration_tolerance=2,
            )

    def test_accepts_hash_at_ten_bits_when_duration_matches(self):
        self.assertIsNotNone(self._lookup("00000000000003ff", scene_duration=122))

    def test_rejects_hash_at_eleven_bits(self):
        self.assertIsNone(self._lookup("00000000000007ff"))

    def test_rejects_duration_outside_two_seconds(self):
        self.assertIsNone(self._lookup("0000000000000000", scene_duration=123))

    def test_rejects_missing_scene_duration_when_duration_is_required(self):
        self.assertIsNone(self._lookup("0000000000000000", scene_duration=0))


if __name__ == "__main__":
    unittest.main()
