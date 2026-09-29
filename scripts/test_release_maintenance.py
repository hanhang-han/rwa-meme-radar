import datetime as dt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.release_maintenance import retention


class ReleaseRetentionTest(unittest.TestCase):
    def backup(self, directory: Path, stamp: str, gib: float):
        path = directory / f"before-{stamp}.tar.gz.sqlite"
        with path.open("wb") as output:
            output.truncate(int(gib * 1024**3))
        return path

    def test_capacity_ceiling_keeps_two_latest_verified_candidates(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            oldest = self.backup(directory, "20260925T091737Z", 1.5)
            older = self.backup(directory, "20260925T132612Z", 2.5)
            previous = self.backup(directory, "20260925T140105Z", 3.7)
            newest = self.backup(directory, "20260925T143541Z", 3.9)
            files, keep = retention(directory, dt.datetime(2026, 9, 25, 15, tzinfo=dt.timezone.utc))
            self.assertEqual(files, [newest, previous, older, oldest])
            self.assertEqual(keep, {newest, previous})

    def test_daily_point_survives_when_under_capacity(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            daily = self.backup(directory, "20260924T110000Z", 1)
            previous = self.backup(directory, "20260925T100000Z", 1)
            newest = self.backup(directory, "20260925T140000Z", 1)
            _, keep = retention(directory, dt.datetime(2026, 9, 25, 15, tzinfo=dt.timezone.utc))
            self.assertEqual(keep, {daily, previous, newest})


if __name__ == "__main__":
    unittest.main()
