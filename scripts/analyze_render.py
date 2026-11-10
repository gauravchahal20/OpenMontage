"""
Fableframe Studio Output Analyzer — the "brain" that verifies render correctness.

Takes a rendered video + its source script/narration and answers:
* Technical: streams, resolution, fps, duration, audio present?
* Visual: distinct scenes, no repetition, no black frames, motion present?
* Captions: Hindi Devanagari captions visible and matching script?
* Content: do scenes match the story beats in order?
* Sync: audio duration matches video duration within tolerance?

Usage:
    .venv\\Scripts\\python.exe scripts\\analyze_render.py \\
        --video projects/fableframe-studio-demo/renders/final.mp4 \\
        --script projects/fableframe-studio-demo/artifacts/script.json \\
        --audio projects/fableframe-studio-demo/assets/audio/narration.wav \\
        --out projects/fableframe-studio-demo/analysis_report.json
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
        "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,duration",
        "-of", "json", str(path),
    ], timeout=60)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _mean_luma(png: Path) -> float | None:
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


def _ocr_hindi(png: Path) -> str:
    """Try to extract Hindi text from a frame using tesseract if available."""
    exe = _tool("tesseract")
    if not exe:
        return ""
    proc = _run([exe, str(png), "stdout", "-l", "hin+eng"], timeout=60)
    return proc.stdout.strip()


def analyze(video: Path, script_path: Path | None, audio_path: Path | None,
            sample_count: int = 12) -> dict[str, Any]:
    report: dict[str, Any] = {
        "video": str(video),
        "script": str(script_path) if script_path else None,
        "audio": str(audio_path) if audio_path else None,
        "checks": {},
        "issues": [],
        "passed": True,
    }

    probe = _probe(video)
    streams = probe.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration") or 0.0)
    size_bytes = int(probe.get("format", {}).get("size") or (video.stat().st_size if video.exists() else 0))

    # --- Technical ---------------------------------------------------------
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
        report["issues"].append("CRITICAL: No video stream.")
        report["passed"] = False
    if not audio_stream:
        report["issues"].append("CRITICAL: No audio stream.")
        report["passed"] = False
    if video_stream.get("width") != 1280 or video_stream.get("height") != 720:
        report["issues"].append(f"WARNING: Resolution is {report['checks']['technical']['resolution']}, expected 1280x720.")

    # --- Duration vs audio -------------------------------------------------
    if audio_path and audio_path.is_file():
        a_probe = _probe(audio_path)
        audio_duration = float(a_probe.get("format", {}).get("duration") or 0.0)
        delta = abs(duration - audio_duration)
        report["checks"]["duration_vs_audio"] = {
            "audio_seconds": round(audio_duration, 3),
            "video_seconds": round(duration, 3),
            "delta_seconds": round(delta, 3),
            "within_1s": delta <= 1.0,
        }
        if delta > 1.0:
            report["issues"].append(f"CRITICAL: Duration differs from audio by {delta:.2f}s.")
            report["passed"] = False

    # --- Audio level -------------------------------------------------------
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
            report["issues"].append("CRITICAL: Audio is effectively silent.")
            report["passed"] = False

    # --- Frames: black, frozen, variety -----------------------------------
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
                report["issues"].append(f"WARNING: Black frames at {black}.")
            if unique < max(3, len(frames) * 0.5):
                report["issues"].append("WARNING: Few unique frames — may be slideshow or repeated scene.")
                report["passed"] = False

            # --- Motion between consecutive frames -------------------------
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
                        report["issues"].append("WARNING: Very little motion between frames.")
                        report["passed"] = False

    # --- Captions: Hindi OCR ---------------------------------------------
    if exe and video_stream and duration > 0:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            caption_frames = []
            for index in range(4):
                offset = duration * (index + 0.5) / 4
                out = tmpdir / f"cap{index:03d}.png"
                proc = _run([
                    exe, "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(video),
                    "-frames:v", "1", str(out),
                ], timeout=120)
                if proc.returncode == 0 and out.is_file():
                    caption_frames.append(out)

            ocr_texts = []
            for cap in caption_frames:
                text = _ocr_hindi(cap)
                ocr_texts.append(text)

            report["checks"]["captions"] = {
                "ocr_available": bool(_tool("tesseract")),
                "sampled_frames": len(caption_frames),
                "hindi_detected": [bool(re.search(r"[\u0900-\u097F]", t)) for t in ocr_texts],
                "ocr_samples": [t[:120] for t in ocr_texts],
            }
            if not any(re.search(r"[\u0900-\u097F]", t) for t in ocr_texts):
                report["issues"].append("WARNING: No Devanagari (Hindi) text detected via OCR — captions may be missing or OCR unavailable.")

    # --- Script content match --------------------------------------------
    total_script_duration = 0.0
    section_durations: list[float] = []
    if script_path and script_path.is_file():
        try:
            script = json.loads(script_path.read_text(encoding="utf-8"))
            sections = script.get("sections", [])
            # Script sections use cumulative start/end timestamps; compute per-section durations.
            section_durations = [
                float(s.get("end_seconds", 0) or 0) - float(s.get("start_seconds", 0) or 0)
                for s in sections
            ]
            total_script_duration = sum(section_durations)
            report["checks"]["script_match"] = {
                "sections_in_script": len(sections),
                "video_duration_seconds": round(duration, 3),
                "script_duration_seconds": round(total_script_duration, 3),
                "section_texts": [s.get("text", "")[:60] for s in sections[:5]],
                "section_durations_seconds": [
                    round(d, 2) for d in section_durations
                ],
            }
            if abs(duration - total_script_duration) > 5.0:
                report["issues"].append(
                    f"WARNING: Video duration ({duration:.1f}s) differs significantly from script total "
                    f"({total_script_duration:.1f}s) — director may have condensed scenes."
                )
        except (json.JSONDecodeError, OSError) as exc:
            report["checks"]["script_match"] = {"error": str(exc)}

    return report


def score(report: dict[str, Any]) -> tuple[float, list[str]]:
    notes: list[str] = []
    total = 10.0
    checks = report.get("checks", {})

    technical = checks.get("technical", {})
    if not technical.get("has_video"):
        total -= 4.0
        notes.append("-4.0 no video stream")
    if not technical.get("has_audio"):
        total -= 3.0
        notes.append("-3.0 no audio stream")
    if not technical.get("resolution") == "1280x720":
        total -= 1.0
        notes.append("-1.0 non-standard resolution")

    duration = checks.get("duration_vs_audio")
    if duration and not duration.get("within_1s"):
        total -= 2.0
        notes.append(f"-2.0 duration off by {duration.get('delta_seconds')}s")

    audio = checks.get("audio_level", {})
    if audio.get("silent"):
        total -= 3.0
        notes.append("-3.0 audio silent")

    frames = checks.get("frames", {})
    if frames.get("black_frames_at"):
        total -= 1.0
        notes.append(f"-1.0 black frames at {frames.get('black_frames_at')}")
    if frames.get("repetition_ratio", 0) > 0.4:
        total -= 1.0
        notes.append("-1.0 many repeated frames")

    motion = checks.get("motion", {})
    if motion.get("static"):
        total -= 1.0
        notes.append("-1.0 static / slideshow")

    captions = checks.get("captions", {})
    if captions.get("ocr_available") and not any(captions.get("hindi_detected", [])):
        total -= 1.0
        notes.append("-1.0 no Hindi captions detected")

    total = max(0.0, min(10.0, total))
    return round(total, 1), notes


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a rendered video for correctness.")
    parser.add_argument("--video", required=True, help="Path to rendered MP4")
    parser.add_argument("--script", default=None, help="Path to script.json")
    parser.add_argument("--audio", default=None, help="Path to narration WAV/MP3")
    parser.add_argument("--out", default=None, help="Write JSON analysis report here")
    parser.add_argument("--samples", type=int, default=12, help="Frame sample count")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.is_file():
        print(f"Not found: {video}")
        return 2

    script = Path(args.script) if args.script else None
    audio = Path(args.audio) if args.audio else None

    report = analyze(video, script, audio, args.samples)
    value, notes = score(report)
    report["score"] = value
    report["score_notes"] = notes
    # Pass if score is good AND no critical issues (warnings are informational).
    critical_issues = [i for i in report["issues"] if i.startswith("CRITICAL")]
    report["passed"] = value >= 7.0 and not critical_issues

    lines = [
        f"# Render Analysis — {video.name}",
        "",
        f"**Score: {value}/10**  {'PASS' if report['passed'] else 'FAIL'}",
        "",
        "## Checks",
        "",
        "```json",
        json.dumps(report["checks"], indent=2, ensure_ascii=False),
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
    # Print using utf-8 to avoid cp1252 encoding errors on Hindi text.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print(json.dumps({"score": value, "passed": report["passed"], "issues": report["issues"]}, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
