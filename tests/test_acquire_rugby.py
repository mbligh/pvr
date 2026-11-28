"""Offline tests of bounded acquisition decisions and safe execution."""
import datetime as dt
import importlib.machinery
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from orchestration.acquire import planning_rows

ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pvr_acquire_rugby", str(ROOT / "acquire_rugby"))
spec = importlib.util.spec_from_loader("pvr_acquire_rugby", loader)
CLI = importlib.util.module_from_spec(spec)
loader.exec_module(CLI)
EPID = "Rugby_Womens_PWR:pwr-289126@mbligh.github.io"
URI = "youtube:A1BcD2EfG3H"
NOW = dt.datetime(2026, 11, 21, 14, tzinfo=dt.timezone.utc)
STEM = "Rugby_Womens_PWR-s2025e101-Harlequins_v_Loughborough_Lightning_20251024"
EP = {
    "_id": EPID, "season": 2025, "episode": 101, "series_id": "Rugby_Womens_PWR",
    "scheduled": dt.datetime(2025, 10, 24, 18, 30, tzinfo=dt.timezone.utc),
    "nfo": "<episodedetails><showtitle>Rugby Womens PWR</showtitle><season>2025</season><episode>101</episode></episodedetails>",
    "nfo_path": "Rugby_Womens_PWR/Season_2025/" + STEM + ".nfo",
}
RELEASE = {
    "_id": URI, "source": "youtube", "episode_id": EPID,
    "available": True, "duration": 8200,
    "metadata": {"live_status": "was_live"},
}


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        real_sports = self.root / "real-sports"
        real_sports.mkdir()
        self.sports = self.root / "sports"
        self.sports.symlink_to(real_sports, target_is_directory=True)
        self.db = MagicMock()
        self.episodes = [dict(EP)]
        self.releases = [dict(RELEASE)]
        self.files = []
        self.jobs = []
        self.db.episodes.find.side_effect = lambda *a, **kw: list(self.episodes)
        def matched_releases(query, *args, **kwargs):
            sources = (query.get("source") or {}).get("$in")
            return [item for item in self.releases
                    if sources is None or item.get("source") in sources]
        self.db.releases.find.side_effect = matched_releases
        self.db.files.find.side_effect = lambda *a, **kw: list(self.files)
        self.db.jobs.find.side_effect = lambda *a, **kw: list(self.jobs)

    def record(self, path, *, fps=60, height=1080, seconds=7200,
               release_uri=URI, moved=False):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video data")
        row = {"_id": "slagg:" + str(path), "host": "slagg", "path": str(path),
               "episode_id": EPID, "size_bytes": path.stat().st_size,
               "mtime_ns": path.stat().st_mtime_ns, "release_uri": release_uri,
               "media": {"duration_seconds": seconds,
                         "video": {"height": height, "fps": fps}}}
        if moved:
            row["file_state"] = "moved"
        self.files.append(row)
        return row

    def rows(self, **kwargs):
        return planning_rows(
            self.db, sports_root=str(self.sports), incoming_root=str(self.incoming),
            hostname="slagg", now=NOW, **kwargs)[0]

    def run_cli(self, *, apply=False, downloader=None, filer=None, **kwargs):
        def unexpected(*args, **kwargs):
            raise AssertionError("Downloader/filer should not be called")
        return CLI.run(
            self.db, apply=apply, hostname="slagg",
            sports_root=str(self.sports), incoming_root=str(self.incoming),
            now=NOW, stream=io.StringIO(), summary_stream=io.StringIO(),
            downloader=downloader or unexpected, filer=filer or unexpected,
            selector=lambda db, eid, host: {
                "preferred_path": str(self.incoming / "pvr-A1BcD2EfG3H.mkv")
            }, **kwargs)

    def test_round_episode_and_season_filters(self):
        self.rows(season=2025, round_no=1, episode_number=101)
        self.db.episodes.find.assert_called_with({
            "series_id": "Rugby_Womens_PWR", "season": 2025,
            "round": 1, "episode": 101,
        })

    def test_globally_sorted_preview_includes_multiple_series_with_season(self):
        fixture_rows = {
            "Rugby_Womens_PWR": [
                {"episode_id": "pwr:new", "scheduled": "2026-11-20T18:00:00+00:00",
                 "season": 2026, "episode": 302, "action": "download_then_file",
                 "source_uri": URI, "incoming": "/tmp/pwr.mkv"},
                {"episode_id": "pwr:old", "scheduled": "2026-11-10T18:00:00+00:00",
                 "season": 2026, "episode": 301, "action": "download_then_file",
                 "source_uri": URI, "incoming": "/tmp/pwr-old.mkv"},
            ],
            "Rugby_Mens_Prem": [
                {"episode_id": "prem:new", "scheduled": "2026-11-19T18:00:00+00:00",
                 "season": 2026, "episode": 403, "action": "download_then_file",
                 "source_uri": "rugby24:prem-1", "incoming": "/tmp/prem.mkv"},
            ],
            "Rugby_Mens_Championship": [
                {"episode_id": "champ:new", "scheduled": "2026-11-21T12:00:00+00:00",
                 "season": 2026, "episode": 201, "action": "download_then_file",
                 "source_uri": "rugby24:champ-1", "incoming": "/tmp/champ.mkv"},
            ],
        }
        observed = []
        def fake_planner(db, *, series_id, season=None, episode_number=None,
                         sources, **kw):
            self.assertEqual(season, 2026)
            self.assertTrue(set(sources).issubset(set(
                __import__("orchestration.source_registry", fromlist=["sources_for"])
                .sources_for(series_id))))
            observed.append(series_id)
            rows = [dict(r) for r in fixture_rows.get(series_id, ())
                    if episode_number is None or r["episode"] == episode_number]
            return rows, {"fixtures": len(rows), "due": len(rows)}
        out = io.StringIO()
        with unittest.mock.patch.object(CLI, "planning_rows", side_effect=fake_planner):
            summary = CLI.run(
                self.db, series_id=None, season=2026, limit=4,
                sports_root=str(self.sports), incoming_root=str(self.incoming),
                stream=out, summary_stream=io.StringIO(), now=NOW)
        rows = [__import__("json").loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([row["episode_id"] for row in rows],
                         ["champ:new", "pwr:new", "prem:new", "pwr:old"])
        self.assertEqual(summary["planned"], 4)
        self.assertEqual(summary["actionable"], 4)
        self.assertIn("Rugby_Mens_Prem", observed)
        self.assertIn("Rugby_Mens_Championship", observed)

    def test_globally_sorted_apply_respects_one_limit_across_series(self):
        rows = {
            "Rugby_Womens_PWR": [
                {"episode_id": "pwr:1", "scheduled": "2026-11-20T12:00:00+00:00",
                 "season": 2026, "episode": 101, "action": "download_then_file",
                 "source_uri": URI, "incoming": str(self.incoming / "pwr.mkv")},
            ],
            "Rugby_Mens_Championship": [
                {"episode_id": "champ:1", "scheduled": "2026-11-21T12:00:00+00:00",
                 "season": 2026, "episode": 201, "action": "download_then_file",
                 "source_uri": "rugby24:champ", "incoming": str(self.incoming / "champ.mkv")},
            ],
            "Rugby_Mens_Prem": [
                {"episode_id": "prem:1", "scheduled": "2026-11-19T12:00:00+00:00",
                 "season": 2026, "episode": 301, "action": "download_then_file",
                 "source_uri": "rugby24:prem", "incoming": str(self.incoming / "prem.mkv")},
            ],
        }
        def fake_planner(db, *, series_id, episode_number=None, **kwargs):
            subset = [dict(x) for x in rows.get(series_id, ())
                      if episode_number is None or x["episode"] == episode_number]
            return subset, {"fixtures": len(subset), "due": len(subset)}
        downloaded = []
        with unittest.mock.patch.object(CLI, "planning_rows", side_effect=fake_planner):
            summary = self.run_cli(
                apply=True, series_id=None, season=2026, limit=2,
                downloader=lambda db, uri, path, **kw: (
                    downloaded.append(uri) or {"status": "downloaded"}),
                filer=lambda *a, **kw: {"status": "filed"})
        self.assertEqual(downloaded, ["rugby24:champ", URI])
        self.assertEqual(summary["planned"], 2)
        self.assertEqual(summary["filed"], 2)

    def test_single_series_explicit_season_still_sorts_newest_first(self):
        new = dict(EP, _id=EPID + ":new", episode=102,
                   scheduled=dt.datetime(2025, 11, 14, 18, 30,
                                         tzinfo=dt.timezone.utc))
        new["nfo"] = EP["nfo"].replace("<episode>101</episode>",
                                       "<episode>102</episode>")
        new["nfo_path"] = EP["nfo_path"].replace("e101", "e102")
        self.episodes.append(new)
        rows = self.rows(season=2025)
        self.assertEqual([row["episode_id"] for row in rows],
                         [new["_id"], EPID])

    def test_preview_is_read_only(self):
        row, = self.rows()
        self.assertEqual(row["action"], "download_then_file")
        self.assertEqual(row["source_uri"], URI)
        self.assertEqual(row["incoming"], str(self.incoming / "pvr-A1BcD2EfG3H.mkv"))
        summary = self.run_cli()
        self.assertEqual(summary["planned"], 1)
        self.assertEqual(summary["actionable"], 1)
        self.assertEqual(summary["downloaded"], 0)
        self.db.jobs.insert_one.assert_not_called()
        self.assertFalse((self.incoming / "pvr-A1BcD2EfG3H.mkv").exists())

    def test_apply_downloads_then_files(self):
        calls = []
        def download(db, uri, path, **kw):
            calls.append(("download", uri, path))
            self.assertTrue(kw["apply"])
            return {"status": "downloaded"}
        def filer(db, path, **kw):
            calls.append(("file", path))
            self.assertTrue(kw["apply"])
            return {"status": "filed"}
        summary = self.run_cli(apply=True, downloader=download, filer=filer)
        self.assertEqual((summary["downloaded"], summary["filed"]), (1, 1))
        self.assertEqual(calls, [
            ("download", URI, str(self.incoming / "pvr-A1BcD2EfG3H.mkv")),
            ("file", str(self.incoming / "pvr-A1BcD2EfG3H.mkv")),
        ])

    def test_existing_eligible_incoming_is_filed_without_download(self):
        local = self.incoming / "old.mkv"
        self.record(local)
        row, = self.rows()
        self.assertEqual((row["action"], row["source"]), ("file", str(local)))
        paths = []
        def filer(db, path, **kw):
            paths.append(path)
            return {"status": "filed"}
        summary = self.run_cli(apply=True, filer=filer)
        self.assertEqual((summary["filed"], summary["downloaded"]), (1, 0))
        self.assertEqual(paths, [str(local)])

    def test_published_low_fps_media_blocks_download(self):
        stored = self.sports / "Rugby_Womens_PWR/Season_2025/alternative.mkv"
        self.record(stored, fps=25, height=720, seconds=3600)
        self.assertEqual(self.rows()[0]["reason"], "already_published")
        self.assertEqual(self.run_cli(apply=True)["planned"], 0)

    def test_stale_published_inventory_still_prevents_duplicate_media(self):
        stored = self.sports / "Rugby_Womens_PWR/Season_2025/alternative.mkv"
        record = self.record(stored)
        record["size_bytes"] += 1
        self.assertEqual(self.rows()[0]["reason"], "already_published")

    def test_unindexed_canonical_file_is_not_overwritten(self):
        target = self.sports / "Rugby_Womens_PWR/Season_2025" / (STEM + ".mkv")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"untracked")
        self.assertEqual(self.rows()[0]["reason"], "canonical_destination_exists")

    def test_local_ineligible_same_release_is_not_redownloaded(self):
        self.record(self.incoming / "short.mkv", seconds=1000)
        self.assertEqual(self.rows()[0]["reason"], "no_new_supported_release")

    def test_existing_incoming_destination_is_skipped(self):
        (self.incoming / "pvr-A1BcD2EfG3H.mkv").write_bytes(b"untracked")
        self.assertEqual(self.rows()[0]["reason"], "incoming_destination_exists")

    def test_multiple_remote_releases_are_downloaded_before_quality_comparison(self):
        self.releases.append(dict(RELEASE, _id="youtube:AbCdEfGhIjK"))
        row, = self.rows()
        self.assertEqual(row["action"], "download_candidates_then_file")
        self.assertEqual(len(row["downloads"]), 2)
        self.assertEqual({x["source_uri"] for x in row["downloads"]},
                         {URI, "youtube:AbCdEfGhIjK"})

    def test_short_release_and_cooldown_are_skipped(self):
        self.releases[0]["duration"] = 1200
        self.assertEqual(self.rows()[0]["reason"], "known_short_release")
        self.releases[0]["duration"] = 8200
        self.jobs = [{"source_uri": URI}]
        self.assertEqual(self.rows()[0]["reason"], "recent_failed_or_running_download")
        self.assertEqual(self.rows(retry_after_hours=0)[0]["action"], "download_then_file")

    def test_recent_cancelled_and_invalid_metadata(self):
        self.episodes[0]["scheduled"] = NOW + dt.timedelta(seconds=1)
        self.assertEqual(self.rows(), [])
        self.episodes[0]["scheduled"] = NOW - dt.timedelta(seconds=1)
        self.assertEqual(self.rows()[0]["action"], "download_then_file")
        self.assertEqual(self.rows(min_age_hours=3), [])
        self.episodes[0]["status"] = "cancelled"
        self.assertEqual(self.rows(), [])
        self.episodes[0]["status"] = "active"
        self.episodes[0]["nfo"] = "<broken"
        self.assertEqual(self.rows()[0]["reason"], "invalid_canonical_metadata")

    def test_moved_source_without_destination_requires_reconciliation(self):
        r = self.record(self.incoming / "old.mkv", moved=True)
        pathlib.Path(r["path"]).unlink()
        self.assertEqual(self.rows()[0]["reason"],
                         "previous_publication_needs_reconciliation")

    def test_apply_failure_stops(self):
        def download(*a, **kw):
            raise RuntimeError("yt-dlp 403")
        summary = self.run_cli(apply=True, downloader=download)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["filed"], 0)

    def test_bbc_and_youtube_remote_candidates_are_both_acquired(self):
        bbc = dict(RELEASE, _id="bbc:m0012345", source="bbc")
        self.releases.append(bbc)
        row, = self.rows()
        self.assertEqual(row["action"], "download_candidates_then_file")
        self.assertEqual([x["source_uri"] for x in row["downloads"]],
                         ["bbc:m0012345", URI])
        calls = []
        def download(db, uri, path, **kw):
            self.assertTrue(kw["allow_existing_episode"])
            self.assertTrue(kw["apply"])
            calls.append(uri)
            return {"status": "downloaded"}
        selected = self.incoming / "pvr-bbc-m0012345.mkv"
        def filer(db, path, **kw):
            self.assertEqual(path, str(selected))
            calls.append("file")
            return {"status": "filed"}
        report = CLI.run(
            self.db, apply=True, hostname="slagg", sources=("bbc", "youtube"),
            sports_root=str(self.sports), incoming_root=str(self.incoming),
            now=NOW, downloader=download, filer=filer,
            selector=lambda db, eid, host: {"preferred_path": str(selected)},
            stream=io.StringIO(), summary_stream=io.StringIO())
        self.assertEqual(calls, ["bbc:m0012345", URI, "file"])
        self.assertEqual((report["downloaded"], report["filed"]), (2, 1))

    def test_bbc_rugbypass_youtube_candidate_limit(self):
        self.releases.append(dict(RELEASE, _id="bbc:m0012345", source="bbc"))
        self.releases.append(dict(RELEASE, _id="rugbypass:1048390", source="rugbypass"))
        row, = self.rows()
        self.assertEqual(row["action"], "download_candidates_then_file")
        self.assertEqual(len(row["downloads"]), 3)
        capped, = self.rows(max_candidates=2)
        self.assertEqual(capped["reason"], "too_many_source_candidates")
        youtube_only, = self.rows(sources=("youtube",))
        self.assertEqual(youtube_only["action"], "download_then_file")

    def test_existing_eligible_local_file_still_compares_new_source(self):
        local = self.incoming / "pvr-A1BcD2EfG3H.mkv"
        self.record(local)
        self.releases.append(dict(RELEASE, _id="bbc:m0012345", source="bbc"))
        row, = self.rows()
        self.assertEqual(row["action"], "download_candidates_then_file")
        self.assertEqual(row["downloads"][0]["source_uri"], "bbc:m0012345")
        self.assertEqual(row["existing_preferred"], str(local))

    def test_partial_multisource_failure_never_files(self):
        self.releases.append(dict(RELEASE, _id="bbc:m0012345", source="bbc"))
        calls = []
        def download(db, uri, path, **kwargs):
            calls.append(uri)
            if uri == URI:
                raise RuntimeError("YouTube 403")
            return {"status": "downloaded"}
        summary = self.run_cli(apply=True, downloader=download)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["filed"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(calls, ["bbc:m0012345", URI])

    def test_unexpected_skip_in_batch_returns_failure(self):
        def download(db, uri, path, **kwargs):
            return {"status": "already_owned"}
        summary = self.run_cli(apply=True, downloader=download)
        self.assertEqual(summary["skipped_on_apply"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_busy_download_site_is_deferred_not_failed(self):
        from orchestration.locks import LockBusy
        def busy(*args, **kwargs):
            raise LockBusy("download provider busy")
        report = self.run_cli(apply=True, downloader=busy)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["filed"], 0)
        self.assertEqual(report["skipped_on_apply"], 1)
        report = self.run_cli(apply=True, downloader=lambda *a, **k: {
            "status": "downloaded"}, filer=lambda *a, **k: {"status": "filed"})
        self.assertEqual(report["filed"], 1)

    def test_worker_has_no_discovery_side_effects_or_refresh_argument(self):
        # Source discovery is owned by run_rugby, not this Atlas-only worker.
        self.assertEqual(self.run_cli()["planned"], 1)
        with self.assertRaises(TypeError):
            self.run_cli(refresh=True)
        self.db.releases.bulk_write.assert_not_called()

    def test_national_rugby_series_only_acquires_rugbypass(self):
        for series_id, root, name in (
                ("Rugby_Mens_Intl", "Rugby_Mens_Intl", "Rugby Mens Intl"),
                ("Rugby_Womens_Intl", "Rugby_Womens_Intl", "Rugby Womens Intl"),
                ("Rugby_Mens_Nations", "Rugby_Mens_Nations", "Rugby Nations Championships")):
            with self.subTest(series=series_id):
                episode_id = f"{series_id}:fixture-123"
                self.episodes[:] = [dict(
                    EP, _id=episode_id, series_id=series_id,
                    season=2026, episode=501,
                    nfo_path=f"{root}/Season_2026/{root}-s2026e501-France_v_South_Africa_20261113.nfo",
                    nfo=f"<episodedetails><showtitle>{name}</showtitle><season>2026</season><episode>501</episode></episodedetails>"
                )]
                self.releases[:] = [dict(
                    RELEASE, _id="rugbypass:1051364", source="rugbypass",
                    episode_id=episode_id)]
                rows = self.rows(series_id=series_id, sources=("rugbypass",))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["action"], "download_then_file")
                self.assertEqual(rows[0]["source_uri"], "rugbypass:1051364")
                self.assertEqual(rows[0]["incoming"],
                                 str(self.incoming / "pvr-rugbypass-1051364.mkv"))
                summary = self.run_cli(series_id=series_id, sources=("rugbypass",))
                self.assertEqual(summary["series"], series_id)
                self.assertEqual(summary["planned"], 1)
                self.db.jobs.insert_one.assert_not_called()
                with self.assertRaisesRegex(ValueError, "RugbyPass only"):
                    self.rows(series_id=series_id, sources=("youtube",))

    def test_episode_lock_refuses_duplicate_episode_only(self):
        from orchestration.locks import episode_lock, LockBusy
        with episode_lock(self.incoming, EPID):
            with self.assertRaises(LockBusy):
                with episode_lock(self.incoming, EPID):
                    pass
            with episode_lock(self.incoming, EPID + "-another"):
                pass
        with episode_lock(self.incoming, EPID):
            pass


if __name__ == "__main__":
    unittest.main()
