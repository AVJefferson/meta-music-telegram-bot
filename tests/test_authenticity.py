from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.authenticity import (
    AuthenticityResult,
    _analyze_spectrum_entire_file,
    _detect_upsampling_entire_file,
    analyze_flac,
    authenticity_from,
    format_line,
    resolved_sample_seconds,
    stamp_identity,
    stamp_report,
    unknown_result,
)
from app.config import Settings
from app.models import Enrichment, Identity, Job, TagHints, TagSet
from app.queue import _preview, process_job, quality, tag_preview
from app.review_ui import format_conflict
from app.songlog import render_songlog
from app.tags import AudioMetrics


class FormatLineTests(unittest.TestCase):
    def test_fake_certain_cutoff_and_hires(self) -> None:
        text = format_line(
            AuthenticityResult(
                verdict="FAKE_CERTAIN",
                score=92,
                cutoff_hz=16200,
                estimated_mp3_bitrate=128,
                hires_verdict="UPSAMPLED",
                flag_hires=True,
            )
        )
        self.assertEqual(
            text,
            "Authenticity: FAKE_CERTAIN (score 92) — cutoff ~16.2 kHz, looks like 128 kbps MP3, Hi-res: UPSAMPLED",
        )

    def test_skipped_and_empty_are_blank(self) -> None:
        self.assertEqual(format_line(None), "")
        self.assertEqual(format_line(AuthenticityResult(verdict="skipped")), "")

    def test_genuine_hires_omitted_from_line(self) -> None:
        text = format_line(
            AuthenticityResult(verdict="AUTHENTIC", score=8, cutoff_hz=22050, hires_verdict="GENUINE_HIRES")
        )
        self.assertEqual(text, "Authenticity: AUTHENTIC (score 8) — cutoff ~22.05 kHz")
        self.assertNotIn("Hi-res", text)

    def test_unknown_keeps_going_text(self) -> None:
        text = format_line(unknown_result("decode failed"))
        self.assertTrue(text.startswith("Authenticity: unknown"))
        self.assertIn("decode failed", text)


class ProbeCopyTests(unittest.TestCase):
    def test_analyze_uses_read_only_probe_not_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "source.flac"
            src.write_bytes(b"fLaC" + b"\x00" * 64)
            seen: dict[str, object] = {}

            def fake_run(path: Path, sample_seconds: float) -> dict:
                seen["path"] = path
                seen["mode"] = path.stat().st_mode
                seen["source"] = src.is_file()
                seen["same"] = path.resolve() == src.resolve()
                return {
                    "verdict": "FAKE_CERTAIN",
                    "score": 92,
                    "cutoff_freq": 16200,
                    "reason": "cliff",
                    "hires_verdict": "UPSAMPLED",
                    "estimated_mp3_bitrate": 128,
                    "is_upsampled": True,
                }

            with patch("app.authenticity._run_analyzer", fake_run):
                result = analyze_flac(src)
            self.assertEqual(result.verdict, "FAKE_CERTAIN")
            self.assertEqual(result.cutoff_hz, 16200)
            self.assertEqual(seen["path"].name, "probe.flac")  # type: ignore[union-attr]
            self.assertFalse(seen["same"])
            self.assertTrue(seen["source"])
            self.assertFalse(stat.S_IMODE(int(seen["mode"])) & 0o222)
            self.assertTrue(src.is_file())
            self.assertFalse((src.parent / "probe.flac").exists())

    def test_analyzer_exception_returns_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "source.flac"
            src.write_bytes(b"fLaC")
            with patch("app.authenticity._run_analyzer", side_effect=RuntimeError("boom")):
                result = analyze_flac(src)
            self.assertEqual(result.verdict, "unknown")
            self.assertIn("boom", result.reason)

    def test_disabled_skips_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "source.flac"
            src.write_bytes(b"fLaC")
            with patch("app.authenticity._run_analyzer") as mocked:
                result = analyze_flac(src, enabled=False)
            mocked.assert_not_called()
            self.assertEqual(result.verdict, "skipped")
            self.assertEqual(format_line(result), "")


class StampAndPreviewTests(unittest.TestCase):
    def test_stamp_and_preview_include_flag(self) -> None:
        identity = Identity(confidence="high", title="Stay")
        result = AuthenticityResult(verdict="FAKE_CERTAIN", score=90, cutoff_hz=16000)
        report = stamp_report({}, result)
        stamp_identity(identity, result)
        self.assertEqual(report["authenticity"]["verdict"], "FAKE_CERTAIN")
        self.assertEqual(authenticity_from(report, identity).verdict, "FAKE_CERTAIN")
        preview = tag_preview(TagSet(title="Stay"), AudioMetrics(172, 16, 44100, 987), authenticity=result)
        self.assertIn("Authenticity: FAKE_CERTAIN", preview)
        self.assertIn("Format: FLAC", preview)
        self.assertIn("<b>Stay</b>", preview)
        queued = _preview(TagSet(title="Stay"), identity, report=report)
        self.assertIn("Authenticity: FAKE_CERTAIN", queued)

    def test_songlog_has_authenticity_block(self) -> None:
        text = render_songlog(
            {
                "file": "a.flac",
                "authenticity": {
                    "verdict": "SUSPICIOUS",
                    "score": 70,
                    "cutoff_hz": 18000,
                    "hires_verdict": "PADDED_DEPTH",
                    "reason": "cliff",
                },
            }
        )
        self.assertIn("== authenticity ==", text)
        self.assertIn("verdict: SUSPICIOUS", text)
        self.assertIn("cutoff_hz: 18000", text)

    def test_conflict_prompt_shows_flag(self) -> None:
        text = format_conflict(
            "track.flac",
            [{"id": "1", "size": 1000, "modified": "2026-01-01"}],
            bit_depth=24,
            sample_rate=96000,
            new_size=2_000_000,
            authenticity="Authenticity: FAKE_CERTAIN (score 92) — cutoff ~16.2 kHz",
        )
        self.assertIn("Authenticity: FAKE_CERTAIN", text)

    def test_quality_tuple_unchanged(self) -> None:
        self.assertEqual(quality(24, 96000), (24, 96000))
        self.assertGreater(quality(24, 96000), quality(16, 44100))


class ProcessJobFlagOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_certain_still_library_commits(self) -> None:
        await self._run_process(side_effect=None, result=AuthenticityResult(verdict="FAKE_CERTAIN", score=99))

    async def test_analyzer_raise_still_library_commits(self) -> None:
        await self._run_process(side_effect=RuntimeError("nope"), result=None)

    async def _run_process(self, *, side_effect, result) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmp_root = root / "tmp"
            tmp_root.mkdir()
            src = root / "in.flac"
            src.write_bytes(b"fLaC" + b"\x00" * 32)
            commits: list[dict] = []
            reviews: list[dict] = []
            identity = Identity(
                confidence="high",
                title="Stay",
                album="Hour",
                artists=["Sam"],
                album_artists=["Sam"],
                mb_recording_id="mbid-1",
                bit_depth=24,
                sample_rate=96000,
                duration=172.0,
            )
            settings = SimpleNamespace(
                tmp_root=tmp_root,
                pending_root=root / "pending",
                authenticity_check=True,
                authenticity_sample_seconds=15.0,
                authenticity_flag_hires=True,
                lastfm_api_key="",
            )
            ctx = SimpleNamespace(
                settings=settings,
                catalog=SimpleNamespace(
                    find_library_by_mbid=lambda *_a, **_k: None,
                    get_pending_review=lambda *_a, **_k: None,
                ),
                drive=SimpleNamespace(),
                http=SimpleNamespace(),
                genre=SimpleNamespace(compose=lambda genre: genre),
                bot=SimpleNamespace(),
                mb=SimpleNamespace(),
            )
            job = Job(1, 1, "General", "fid", "stay.flac", 9, local_path=str(src))

            async def fake_edit(*_a, **_k):
                return 9

            async def fake_enrich(*_a, **_k):
                return Enrichment()

            async def fake_commit(*_args, **kwargs):
                commits.append(kwargs)

            async def fake_review(*_args, **_kwargs):
                reviews.append({"hit": True})

            analyze_kw = {"side_effect": side_effect} if side_effect else {"return_value": result}
            with (
                patch("app.queue.edit_status", fake_edit),
                patch("app.queue.read_hints", return_value=TagHints(title="Stay", filename="stay.flac")),
                patch("app.queue.read_cover", return_value=(None, None)),
                patch("app.queue.identify_file", return_value=identity),
                patch("app.queue.enrich", fake_enrich),
                patch("app.queue.analyze_flac", **analyze_kw),
                patch("app.queue._library_commit_with_cover", fake_commit),
                patch("app.queue.start_tag_review", fake_review),
            ):
                await process_job(job, ctx)

            self.assertEqual(len(commits), 1)
            self.assertEqual(reviews, [])
            auth = commits[0]["report"]["authenticity"]
            if side_effect:
                self.assertEqual(auth["verdict"], "unknown")
            else:
                self.assertEqual(auth["verdict"], "FAKE_CERTAIN")


class SyntheticFlacTests(unittest.TestCase):
    def test_soundfile_brick_wall_probe(self) -> None:
        try:
            import numpy as np
            import soundfile as sf
        except ImportError:
            self.skipTest("numpy/soundfile missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brick.flac"
            sample_rate = 44100
            tone = np.sin(2 * np.pi * 1000 * np.linspace(0, 0.2, int(sample_rate * 0.2), endpoint=False))
            sf.write(path, (0.2 * tone).astype(np.float32), sample_rate, format="FLAC", subtype="PCM_16")
            seen: dict[str, bool] = {}

            def fake_run(probe: Path, sample_seconds: float) -> dict:
                seen["probe"] = probe.is_file()
                seen["source"] = path.is_file()
                return {"verdict": "WARNING", "score": 40, "cutoff_freq": 16000, "reason": "synthetic"}

            with patch("app.authenticity._run_analyzer", fake_run):
                result = analyze_flac(path)
            self.assertEqual(result.verdict, "WARNING")
            self.assertTrue(seen.get("probe"))
            self.assertTrue(seen.get("source"))
            self.assertTrue(path.is_file())


class FullFileSampleTests(unittest.TestCase):
    def test_settings_accepts_minus_one(self) -> None:
        self.assertEqual(Settings._normalize_auth_sample_seconds(-1), -1.0)
        self.assertEqual(Settings._normalize_auth_sample_seconds("-1"), -1.0)
        self.assertEqual(Settings._normalize_auth_sample_seconds(20), 20.0)
        self.assertEqual(Settings._normalize_auth_sample_seconds(0), 15.0)
        self.assertEqual(Settings._normalize_auth_sample_seconds(-2), 15.0)
        self.assertEqual(Settings._normalize_auth_sample_seconds("nope"), 15.0)

    def test_resolved_sample_seconds_uses_file_duration(self) -> None:
        path = Path("unused.flac")
        self.assertEqual(resolved_sample_seconds(path, 15), 15.0)
        with patch("app.authenticity.read_audio_metrics", return_value=AudioMetrics(duration=172.5)):
            self.assertEqual(resolved_sample_seconds(path, -1), 172.5)
        with patch("app.authenticity.read_audio_metrics", return_value=AudioMetrics(duration=0)):
            self.assertEqual(resolved_sample_seconds(path, -1), 1_000_000_000.0)
        with patch("app.authenticity.read_audio_metrics", side_effect=RuntimeError("bad flac")):
            self.assertEqual(resolved_sample_seconds(path, -1), 1_000_000_000.0)

    def test_minus_one_passes_file_duration_into_analyzer(self) -> None:
        seen: dict[str, float] = {}

        class FakeAnalyzer:
            def __init__(self, sample_duration: float = 30.0, deep: bool = False) -> None:
                seen["duration"] = sample_duration

            def analyze_file(self, path: Path) -> dict:
                seen["name"] = path.name
                return {"verdict": "AUTHENTIC", "score": 0}

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "source.flac"
            src.write_bytes(b"fLaC")
            with (
                patch("app.authenticity.read_audio_metrics", return_value=AudioMetrics(duration=172.0)),
                patch("flac_detective.FLACAnalyzer", FakeAnalyzer),
                patch("app.authenticity._entire_file_analysis") as full_ctx,
            ):
                full_ctx.return_value.__enter__ = lambda *a: None
                full_ctx.return_value.__exit__ = lambda *a: False
                result = analyze_flac(src, sample_seconds=-1)
        self.assertEqual(result.verdict, "AUTHENTIC")
        self.assertEqual(seen["duration"], 172.0)
        full_ctx.assert_called_once()

    def test_positive_seconds_skips_full_file_patch(self) -> None:
        class FakeAnalyzer:
            def __init__(self, sample_duration: float = 30.0, deep: bool = False) -> None:
                self.sample_duration = sample_duration

            def analyze_file(self, path: Path) -> dict:
                return {"verdict": "AUTHENTIC", "score": 0}

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "source.flac"
            src.write_bytes(b"fLaC")
            with (
                patch("flac_detective.FLACAnalyzer", FakeAnalyzer),
                patch("app.authenticity._entire_file_analysis") as full_ctx,
            ):
                analyze_flac(src, sample_seconds=15)
        full_ctx.assert_not_called()

    def test_spectrum_ffts_every_frame(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy missing")
        frames = 20_000
        audio = np.full((frames, 1), 0.1, dtype=np.float32)
        cache = SimpleNamespace(get_full_audio=lambda: (audio, 1000))
        seen: dict[str, int] = {}

        def fake_cutoff(freq, mag, sr):
            seen["bins"] = len(freq)
            return 400.0

        with (
            patch("flac_detective.analysis.spectrum.detect_cutoff", fake_cutoff),
            patch("flac_detective.analysis.spectrum.calculate_high_frequency_energy", return_value=0.01),
        ):
            cutoff, _energy, std, _floor = _analyze_spectrum_entire_file(Path("x.flac"), cache=cache)
        self.assertEqual(cutoff, 400.0)
        self.assertEqual(std, 0.0)
        self.assertEqual(seen["bins"], frames // 2 + 1)

    def test_hires_wrapper_keeps_audio_past_30s(self) -> None:
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy missing")
        seen: dict[str, int] = {}

        def orig(audio, samplerate):
            data = audio
            if data.ndim > 1:
                data = np.mean(data, axis=1)
            data = np.asarray(data[: int(30.0 * samplerate)], dtype=np.float64)
            seen["frames"] = len(data)
            return {"is_upsampled": False}

        wrapped = _detect_upsampling_entire_file(orig)
        sr = 1000
        audio = np.ones((80_000, 1), dtype=np.float32)
        wrapped(audio, sr)
        self.assertEqual(seen["frames"], 80_000)
