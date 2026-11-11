import tempfile, unittest
from pathlib import Path
import imagevault as iv


class ImageVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src, self.dst = self.tmp / "src", self.tmp / "out"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "IMG_20230704_101010.jpg").write_bytes(b"A" * 100)
        (self.src / "sub" / "copy.jpg").write_bytes(b"A" * 100)      # duplicate
        (self.src / "photo.png").write_bytes(b"B" * 50)
        (self.src / "notes.txt").write_text("not an image")

    def test_scan_and_duplicates(self):
        recs, skipped, _ = iv.scan_directory(self.src)
        self.assertEqual((len(recs), skipped), (3, 1))
        groups, _ = iv.find_duplicates(recs)
        self.assertEqual(len(groups), 1)

    def test_filename_date(self):
        recs, _, _ = iv.scan_directory(self.src)
        r = next(r for r in recs if r.path.name.startswith("IMG_2023"))
        self.assertEqual((r.date_source, r.taken.year, r.taken.month), ("filename", 2023, 7))

    def test_preview_changes_nothing(self):
        iv.main(["organize", str(self.src), str(self.dst)])
        self.assertFalse(self.dst.exists())

    def test_apply_and_undo(self):
        iv.main(["organize", str(self.src), str(self.dst), "--apply", "--yes"])
        self.assertEqual(len([p for p in self.dst.rglob("*") if p.is_file() and not p.name.startswith(".")]), 3)
        self.assertTrue((self.dst / iv.DUP_FOLDER).exists())
        iv.main(["undo", str(self.dst)])
        self.assertEqual([p for p in self.dst.rglob("*") if p.is_file()], [])

    def test_invalid_source(self):
        self.assertEqual(iv.main(["organize", str(self.tmp / "nope"), str(self.dst)]), 2)


if __name__ == "__main__":
    unittest.main()
