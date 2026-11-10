"""Measure a rendered video and score it.

Run this on any finished MP4 to know what is actually wrong with it, instead of
guessing from the tool's success message. It answers the questions that matter
for a narrated long video:

* does the file carry both streams, at the right resolution and frame rate?
* does its duration match the narration it was built from?
* is the audio actually there, and audible rather than silent?
* does anything freeze, repeat, or go black?
* is there enough motion, or does it read as a slideshow?

Usage::

    .venv\\Scripts\\python.exe scripts\\quality_check_video.py <video.mp4> \\
        [--narration <audio>] [--shots <shot.json>] [--out <report.md>]

Exit code is 0 when the score is at least --min-score (default 7.0), so it can
gate a compose stage.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _probe(path: Path) -> dict[str, Any]:
    exe = _tool("ffprobe")
    if not exe:
        return {}
    proc = _run([
        exe, "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames",
        "-of", "json", str(path),
    ], timeout=60)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _mean_luma(png: Path) -> float | None:
    """Average luminance of a frame, via ffmpeg's signalstats."""
    exe = _tool("ffmpeg")
    if not exe:
        return None
    proc = _run([
        exe, "-v", "error", "-i", str(png),
        "-vf", "signalstats,metadata=print:file=-",
        "-frames:v", "1", "-f", "null", "-",
    ], timeout=120)
    match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", proc.stdout or "")
    return float(match.group(1)) if match else None


def _frame_hash(png: Path) -> str:
    import hashlib

    return hashlib.sha256(png.read_bytes()).hexdigest()


def analyse(video: Path, narration: Path | None, sample_count: int = 12) -> dict[str, Any]:
    report: dict[str, Any] = {"video": str(video), "checks": {}, "issues": []}
    probe = _probe(video)
    streams = probe.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration") or 0.0)
    size_bytes = int(probe.get("format", {}).get("size") or (video.stat().st_size if video.exists() else 0))

    report["checks"]["technical"] = {
        "has_video": bool(video_stream),
        "has_audio": bool(audio_stream),
        "resolution": f"{video_stream.get('width')}x{video_stream.get('height')}",
        "fps": video_stream.get("r_frame_rate"),
        "codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "duration_seconds": round(duration, 3),
        "size_bytes": size_bytes,
    }
    if not video_stream:
        report["issues"].append("The file has no video stream.")
    if not audio_stream:
        report["issues"].append("The file has no audio stream.")

    # --- duration vs narration -------------------------------------------
    if narration and narration.is_file():
        nar = _probe(narration)
        nar_duration = float(nar.get("format", {}).get("duration") or 0.0)
        delta = abs(duration - nar_duration)
        report["checks"]["duration_vs_narration"] = {
            "narration_seconds": round(nar_duration, 3),
            "delta_seconds": round(delta, 3),
            "within_1s": delta <= 1.0,
        }
        if delta > 1.0:
            report["issues"].append(
                f"Duration differs from the narration by {delta:.2f}s "
                f"({duration:.2f}s vs {nar_duration:.2f}s)."
            )

    # --- audio level ------------------------------------------------------
    exe = _tool("ffmpeg")
    if exe and audio_stream:
        vol = _run([
            exe, "-v", "info", "-i", str(video), "-map", "0:a:0",
            "-af", "volumedetect", "-f", "null", "-",
        ], timeout=300)
        mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", vol.stderr or "")
        peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", vol.stderr or "")
        mean_db = float(mean.group(1)) if mean else None
        report["checks"]["audio_level"] = {
            "mean_db": mean_db,
            "peak_db": float(peak.group(1)) if peak else None,
            "silent": mean_db is not None and mean_db < -55,
        }
        if mean_db is not None and mean_db < -55:
            report["issues"].append(f"The audio is effectively silent (mean {mean_db:.1f} dB).")

    # --- frames: black, frozen, variety ----------------------------------
    if exe and video_stream and duration > 0:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            frames = []
            for index in range(sample_count):
                offset = duration * (index + 0.5) / sample_count
                out = tmpdir / f"f{index:03d}.png"
                proc = _run([
                    exe, "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(video),
                    "-frames:v", "1", str(out),
                ], timeout=120)
                if proc.returncode == 0 and out.is_file():
                    frames.append((offset, out))

            hashes = [_frame_hash(png) for _, png in frames]
            lumas = [_mean_luma(png) for _, png in frames]
            unique = len(set(hashes))
            black = [round(o, 2) for (o, _), luma in zip(frames, lumas) if luma is not None and luma < 16]

            report["checks"]["frames"] = {
                "sampled": len(frames),
                "unique_frames": unique,
                "repetition_ratio": round(1 - unique / max(len(frames), 1), 3),
                "black_frames_at": black,
                "mean_luma": round(
                    sum(l for l in lumas if l is not None) / max(1, len([l for l in lumas if l is not None])),
                    1,
                ) if any(l is not None for l in lumas) else None,
            }
            if black:
                report["issues"].append(f"Black frames at {black}.")
            if unique < max(3, len(frames) * 0.6):
                report["issues"].append(
                    f"Only {unique} of {len(frames)} sampled frames are unique — "
                    "the video repeats or holds still."
                )

            # motion: difference between consecutive sampled frames
            if len(frames) >= 2:
                deltas = []
                for (offset_a, a), (offset_b, b) in zip(frames, frames[1:]):
                    proc = _run([
                        exe, "-v", "error", "-i", str(a), "-i", str(b),
                        "-filter_complex", "blend=all_mode=difference,signalstats,metadata=print:file=-",
                        "-f", "null", "-",
                    ], timeout=120)
                    match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", proc.stdout or "")
                    if match:
                        deltas.append(float(match.group(1)))
                if deltas:
                    mean_delta = sum(deltas) / len(deltas)
                    report["checks"]["motion"] = {
                        "mean_frame_difference": round(mean_delta, 3),
                        "static": mean_delta < 0.6,
                    }
                    if mean_delta < 0.6:
                        report["issues"].append(
                            f"Very little motion between frames (mean difference {mean_delta:.2f}) — "
                            "it reads as a slideshow."
                        )

    return report


def score(report: dict[str, Any]) -> tuple[float, list[str]]:
    """Score out of 10 and explain the deduction."""
    notes: list[str] = []
    checks = report.get("checks", {})
    total = 10.0

    technical = checks.get("technical", {})
    if not technical.get("has_video"):
        total -= 5.0
        notes.append("-5.0 no video stream")
    if not technical.get("has_audio"):
        total -= 2.5
        notes.append("-2.5 no audio stream")
    if technical.get("resolution") not in ("1920x1080", "1280x720", "None"):
        total -= 1.0
        notes.append(f"-1.0 unusual resolution {technical.get('resolution')}")

    duration = checks.get("duration_vs_narration")
    if duration and not duration.get("within_1s"):
        total -= 2.0
        notes.append(f"-2.0 duration off by {duration.get('delta_seconds')}s")

    audio = checks.get("audio_level", {})
    if audio.get("silent"):
        total -= 3.0
        notes.append("-3.0 audio is silent")

    frames = checks.get("frames", {})
    if frames.get("black_frames_at"):
        total -= 1.5
        notes.append(f"-1.5 black frames at {frames.get('black_frames_at')}")
    if frames.get("repetition_ratio", 0) > 0.4:
        total -= 1.5
        notes.append(f"-1.5 {frames.get('repetition_ratio')} of sampled frames repeat")
    elif frames.get("repetition_ratio", 0) > 0.2:
        total -= 0.5
        notes.append(f"-0.5 some frames repeat ({frames.get('repetition_ratio')})")

    motion = checks.get("motion", {})
    if motion.get("static"):
        total -= 1.5
        notes.append("-1.5 very little motion — reads as a slideshow")

    total = max(0.0, min(10.0, total))
    return round(total, 1), notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--narration", default=None)
    parser.add_argument("--shots", default=None, help="JSON shot list, for shot-level checks")
    parser.add_argument("--out", default=None, help="Write a markdown report here")
    parser.add_argument("--min-score", type=float, default=7.0)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()

    video = Path(args.video)
    if not video.is_file():
        print(f"Not found: {video}")
        return 2

    report = analyse(video, Path(args.narration) if args.narration else None, args.samples)
    value, notes = score(report)
    report["score"] = value
    report["score_notes"] = notes

    lines = [
        f"# Video quality check — {video.name}",
        "",
        f"**Score: {value}/10**",
        "",
        "## Measurements",
        "",
        "```json",
        json.dumps(report["checks"], indent=2),
        "```",
        "",
        "## Issues",
        "",
    ]
    lines.extend([f"- {issue}" for issue in report["issues"]] or ["- none found"])
    lines += ["", "## Score breakdown", ""]
    lines.extend(notes or ["- no deductions"])
    lines.append("")

    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print(json.dumps({"score": value, "issues": report["issues"]}, indent=2))
    return 0 if value >= args.min_score else 1


if __name__ == "__main__":
    sys.exit(main())
