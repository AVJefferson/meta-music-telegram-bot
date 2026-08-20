from __future__ import annotations

import contextlib
import logging
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.tags import read_audio_metrics

log = logging.getLogger(__name__)

HIRES_FLAGS = frozenset({"UPSAMPLED", "PADDED_DEPTH", "UPSAMPLED_AND_PADDED"})
SKIPPED = "skipped"
UNKNOWN = "unknown"
_FULL_FILE_DURATION = 1_000_000_000.0


def resolved_sample_seconds(path: Path, sample_seconds: float) -> float:
    if sample_seconds >= 0:
        return float(sample_seconds)
    try:
        duration = float(read_audio_metrics(path).duration or 0.0)
    except Exception:
        duration = 0.0
    if duration <= 0:
        return _FULL_FILE_DURATION
    return duration


@dataclass(frozen=True)
class AuthenticityResult:
    verdict: str = UNKNOWN
    score: int | None = None
    cutoff_hz: float | None = None
    reason: str = ""
    hires_verdict: str = ""
    estimated_mp3_bitrate: int | None = None
    is_upsampled: bool = False
    flag_hires: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def unknown_result(reason: str = "", *, flag_hires: bool = True) -> AuthenticityResult:
    return AuthenticityResult(verdict=UNKNOWN, reason=reason, flag_hires=flag_hires)


def skipped_result(*, flag_hires: bool = True) -> AuthenticityResult:
    return AuthenticityResult(verdict=SKIPPED, flag_hires=flag_hires)


def from_dict(raw: object, *, flag_hires: bool | None = None) -> AuthenticityResult | None:
    if not isinstance(raw, dict) or not raw:
        return None
    verdict = str(raw.get("verdict") or "").strip() or UNKNOWN
    show_hires = raw.get("flag_hires") if flag_hires is None else flag_hires
    return AuthenticityResult(
        verdict=verdict,
        score=_int_or_none(raw.get("score")),
        cutoff_hz=_float_or_none(raw.get("cutoff_hz") if "cutoff_hz" in raw else raw.get("cutoff_freq")),
        reason=str(raw.get("reason") or "").strip(),
        hires_verdict=str(raw.get("hires_verdict") or "").strip(),
        estimated_mp3_bitrate=_int_or_none(raw.get("estimated_mp3_bitrate")),
        is_upsampled=bool(raw.get("is_upsampled")),
        flag_hires=True if show_hires is None else bool(show_hires),
    )


def authenticity_from(*sources: object) -> AuthenticityResult | None:
    for source in sources:
        if isinstance(source, AuthenticityResult):
            return source
        payload = source
        if hasattr(source, "source_report"):
            payload = getattr(source, "source_report", None)
        if isinstance(payload, dict):
            found = from_dict(payload.get("authenticity"))
            if found is not None:
                return found
    return None


def stamp_report(report: dict[str, Any], result: AuthenticityResult) -> dict[str, Any]:
    out = dict(report)
    out["authenticity"] = result.to_dict()
    return out


def stamp_identity(identity: object, result: AuthenticityResult) -> None:
    current = getattr(identity, "source_report", None)
    src = dict(current) if isinstance(current, dict) else {}
    src["authenticity"] = result.to_dict()
    identity.source_report = src


def format_line(auth: object, *, flag_hires: bool | None = None) -> str:
    result = auth if isinstance(auth, AuthenticityResult) else from_dict(auth, flag_hires=flag_hires)
    if result is None or result.verdict in {"", SKIPPED}:
        return ""
    head = f"Authenticity: {result.verdict}"
    if result.score is not None:
        head += f" (score {result.score})"
    details: list[str] = []
    cutoff = _format_cutoff(result.cutoff_hz)
    if cutoff:
        details.append(f"cutoff ~{cutoff}")
    if result.estimated_mp3_bitrate:
        details.append(f"looks like {result.estimated_mp3_bitrate} kbps MP3")
    elif result.reason:
        details.append(_clip_reason(result.reason))
    show_hires = result.flag_hires if flag_hires is None else flag_hires
    hires = result.hires_verdict.upper()
    if show_hires and hires in HIRES_FLAGS:
        details.append(f"Hi-res: {hires}")
    if not details:
        return head
    return f"{head} — {', '.join(details)}"


def analyze_flac(
    path: Path,
    *,
    enabled: bool = True,
    sample_seconds: float = 15.0,
    flag_hires: bool = True,
) -> AuthenticityResult:
    if not enabled:
        return skipped_result(flag_hires=flag_hires)
    probe = path.parent / "probe.flac"
    try:
        shutil.copy2(path, probe)
        probe.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        raw = _run_analyzer(probe, sample_seconds)
        return _from_analyzer(raw, flag_hires=flag_hires)
    except Exception as exc:
        log.warning("authenticity analysis failed path=%s: %s", path, exc)
        return unknown_result(str(exc), flag_hires=flag_hires)
    finally:
        probe.unlink(missing_ok=True)
        Path(str(probe) + ".bak").unlink(missing_ok=True)
        Path(str(probe) + ".corrupted.bak").unlink(missing_ok=True)


def _run_analyzer(path: Path, sample_seconds: float) -> dict[str, Any]:
    from flac_detective import FLACAnalyzer

    duration = resolved_sample_seconds(path, sample_seconds)
    analyzer = FLACAnalyzer(sample_duration=duration)
    if sample_seconds >= 0:
        result = analyzer.analyze_file(path)
    else:
        log.info("authenticity full-file analysis duration=%.1fs path=%s", duration, path.name)
        with _entire_file_analysis():
            result = analyzer.analyze_file(path)
    if not isinstance(result, dict):
        raise TypeError(f"flac-detective returned {type(result)!r}, expected dict")
    return result


@contextlib.contextmanager
def _entire_file_analysis():
    from flac_detective.analysis import analyzer as analyzer_mod
    from flac_detective.analysis import hires as hires_mod
    from flac_detective.analysis import spectrum as spectrum_mod

    orig_analyzer_spectrum = analyzer_mod.analyze_spectrum
    orig_spectrum = spectrum_mod.analyze_spectrum
    orig_floor = spectrum_mod.compute_residual_floor_db
    orig_hires = hires_mod.detect_upsampling

    def full_floor(full_audio, samplerate, max_seconds: float = 30.0):
        return orig_floor(full_audio, samplerate, max_seconds=_FULL_FILE_DURATION)

    analyzer_mod.analyze_spectrum = _analyze_spectrum_entire_file
    spectrum_mod.analyze_spectrum = _analyze_spectrum_entire_file
    spectrum_mod.compute_residual_floor_db = full_floor
    hires_mod.detect_upsampling = _detect_upsampling_entire_file(orig_hires)
    try:
        yield
    finally:
        analyzer_mod.analyze_spectrum = orig_analyzer_spectrum
        spectrum_mod.analyze_spectrum = orig_spectrum
        spectrum_mod.compute_residual_floor_db = orig_floor
        hires_mod.detect_upsampling = orig_hires


def _analyze_spectrum_entire_file(filepath: Path, sample_duration: float = 30.0, cache: Any = None):
    """One Hann+rFFT over every decoded frame. Detective splits files >90s into 3 windows."""
    import numpy as np
    from flac_detective.analysis.spectrum import (
        calculate_high_frequency_energy,
        compute_residual_floor_db,
        detect_cutoff,
    )
    from flac_detective.analysis.window_cache import get_hann_window
    from scipy.fft import rfft, rfftfreq, set_workers

    if cache is None:
        from flac_detective.analysis.audio_cache import AudioCache

        cache = AudioCache(filepath)
    _ = sample_duration
    full_audio, samplerate = cache.get_full_audio()
    if full_audio.ndim > 1:
        data = np.mean(full_audio, axis=1) if full_audio.shape[1] > 1 else full_audio[:, 0]
    else:
        data = full_audio
    window = get_hann_window(len(data))
    with set_workers(1):
        fft_vals = rfft(data * window)
    fft_freq = rfftfreq(len(data), 1 / samplerate)
    magnitude = np.abs(fft_vals)
    magnitude_db = 20 * np.log10(magnitude + 1e-10)
    cutoff_freq = detect_cutoff(fft_freq, magnitude_db, samplerate)
    energy_ratio = calculate_high_frequency_energy(fft_freq, magnitude)
    nyquist = samplerate / 2.0
    residual_floor_db = float("nan")
    if 0.90 * nyquist <= cutoff_freq < 0.95 * nyquist:
        residual_floor_db = compute_residual_floor_db(
            full_audio, samplerate, max_seconds=_FULL_FILE_DURATION
        )
    return cutoff_freq, energy_ratio, 0.0, residual_floor_db


def _detect_upsampling_entire_file(original):
    def wrapped(audio, samplerate):
        import numpy as np

        data = np.asarray(audio)
        if data.ndim > 1:
            data = np.mean(data, axis=1)
        else:
            data = np.reshape(data, -1)

        class _NoTimeCap(np.ndarray):
            def __getitem__(self, item):
                if isinstance(item, slice):
                    return np.ndarray.__getitem__(self, slice(item.start, None, item.step))
                return np.ndarray.__getitem__(self, item)

        return original(np.asarray(data, dtype=np.float64).view(_NoTimeCap), samplerate)

    return wrapped


def _from_analyzer(raw: dict[str, Any], *, flag_hires: bool) -> AuthenticityResult:
    hires = str(raw.get("hires_verdict") or "").strip()
    if not hires:
        if raw.get("is_upsampled") and raw.get("is_fake_high_res"):
            hires = "UPSAMPLED"
        elif raw.get("is_fake_high_res"):
            hires = "PADDED_DEPTH"
    return AuthenticityResult(
        verdict=str(raw.get("verdict") or UNKNOWN).strip() or UNKNOWN,
        score=_int_or_none(raw.get("score")),
        cutoff_hz=_float_or_none(raw.get("cutoff_freq") if raw.get("cutoff_freq") is not None else raw.get("cutoff_hz")),
        reason=str(raw.get("reason") or "").strip(),
        hires_verdict=hires,
        estimated_mp3_bitrate=_int_or_none(raw.get("estimated_mp3_bitrate")),
        is_upsampled=bool(raw.get("is_upsampled")),
        flag_hires=flag_hires,
    )


def _format_cutoff(value: float | None) -> str:
    if value is None:
        return ""
    hz = float(value)
    if hz >= 1000:
        return f"{hz / 1000:g} kHz"
    return f"{hz:g} Hz"


def _clip_reason(text: str, limit: int = 80) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _int_or_none(value: object) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
