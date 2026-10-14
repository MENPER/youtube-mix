import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class MediaHelpersTest(unittest.TestCase):
    def test_safe_title_removes_extension_and_limits_length(self):
        self.assertEqual(main.safe_title("A track.wav"), "A track")
        self.assertEqual(len(main.safe_title("x" * 200)), 120)

    def test_read_library_ignores_invalid_ids_and_missing_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            (library / "index.json").write_text(json.dumps([
                {"id": "a" * 32, "title": "valid"},
                {"id": "../../bad", "title": "invalid"},
                "not a record",
            ]))
            (library / f"{'a' * 32}.m4a").touch()
            with patch.object(main, "LIBRARY_DIR", library):
                self.assertEqual(main.read_library(), [{"id": "a" * 32, "title": "valid"}])

    def test_read_library_tolerates_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            (library / "index.json").write_text("{")
            with patch.object(main, "LIBRARY_DIR", library):
                self.assertEqual(main.read_library(), [])


if __name__ == "__main__":
    unittest.main()
