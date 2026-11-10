"""Build a long video from a narration track.

Every hosted video model generates *short* clips — Google Veo 3.1 is 8 seconds
at 1080p — so a long video is not one generation, it is a sequence of clips
that must be planned, generated, fitted to the narration and joined. This tool
does that whole chain in one call:

    narration audio
      -> transcript            (word timings, local Whisper)
      -> shot list             (one shot per narration span)
      -> one clip per shot     (Veo / ComfyUI still + Ken Burns / mock)
      -> each clip fitted      (trimmed, slowed or padded to its span)
      -> joined                (concat, optional crossfade)
      -> narration muxed back  (your audio is the audio that ships)

Nothing about the narration is regenerated: the audio you supply is the audio
that ends up in the video, and every clip is aligned to the words it covers.

Clip providers
--------------
``veo``    Google's Veo 3.1 — real motion, native audio, costs money
           (~$0.40 per generated second, so an 8s clip is ~$3.20).
``still``  One ComfyUI image per shot animated with a Ken Burns move. Free and
           local; uses the SD 1.5 checkpoint already on the machine.
``mock``   Flat colour clips. No API key, no GPU — for wiring tests.

Cost note for ``veo``: the bill follows *generated clip seconds*, not final
runtime. A 60-second video needs ~8 clips of 8s = 64 generated seconds
(~$25.60). A 5-minute video needs ~38 clips = 304 generated seconds (~$122).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

# A minimal Stable Diffusion 1.5 text-to-image graph. Built here rather than
# shipped as a file so the checkpoint name can be discovered from the running
# ComfyUI server instead of hard-coded.
SD15_TXT2IMG = {
    "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["3", 1]}},
    "5": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, low quality, watermark, text, deformed", "clip": ["3", 1]},
    },
    "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 432, "batch_size": 1}},
    "7": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 20,
            "cfg": 7.0,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "denoise": 1.0,
            "model": ["3", 0],
            "positive": ["4", 0],
            "negative": ["5", 0],
            "latent_image": ["6", 0],
        },
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "longform", "images": ["8", 0]}},
}

# The image-to-image variant: the previous shot's frame is the starting point,
# so the next shot continues from it instead of inventing a new scene. This is
# the local equivalent of Google Flow's "Scenes" — a subject that stays the
# same across shots.
SD15_IMG2IMG = {
    "3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["3", 1]}},
    "5": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, low quality, watermark, text, deformed", "clip": ["3", 1]},
    },
    "10": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["3", 2]}},
    "7": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0, "steps": 20, "cfg": 7.0,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 0.55,
            "model": ["3", 0], "positive": ["4", 0],
            "negative": ["5", 0], "latent_image": ["11", 0],
        },
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "longform", "images": ["8", 0]}},
}

# Words per shot when the transcript is too coarse to split by sentence.
MAX_SHOT_SECONDS = 8.0
MIN_SHOT_SECONDS = 2.5


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def _ffprobe_json(path: Path, entries: str) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    proc = _run(
        [exe, "-v", "error", "-show_entries", entries, "-of", "json", str(path)], timeout=60
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _media_duration(path: Path) -> float:
    data = _ffprobe_json(path, "format=duration")
    try:
        return float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।])\s+|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


class LongformVideo(BaseTool):
    name = "longform_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "longform"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.HYBRID

    dependencies = ["cmd:ffmpeg"]
    install_instructions = (
        "FFmpeg is required for the join/mux stage:\n"
        "  winget install FFmpeg\n"
        "For the `veo` provider also configure Google credentials "
        "(GOOGLE_API_KEY or GEMINI_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS "
        "for Vertex AI). For the `still` provider, run ComfyUI on 127.0.0.1:8188."
    )

    agent_skills = ["ai-video-gen", "ffmpeg", "video-toolkit", "text-to-speech"]

    capabilities = [
        "long_form_video_from_audio",
        "shot_planning",
        "clip_alignment",
        "clip_sequencing",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
    }
    best_for = [
        "long narrated videos assembled from generated shots",
        "turning a finished narration track into a finished video",
    ]
    not_good_for = [
        "offline-only runs with no Google key and no ComfyUI (use provider=mock)",
    ]
    fallback_tools = ["veo_video", "video_stitch", "video_trimmer", "audio_mixer"]

    input_schema = {
        "type": "object",
        "required": ["audio_path", "output_path"],
        "properties": {
            "audio_path": {
                "type": "string",
                "description": "Narration track (wav/mp3/m4a/flac/ogg). It is muxed onto the video unchanged.",
            },
            "output_path": {"type": "string", "description": "Where to write the finished MP4."},
            "provider": {
                "type": "string",
                "enum": ["veo", "flow", "still", "mock"],
                "default": "veo",
                "description": (
                    "Where each shot's clip comes from. `veo` = Google Veo 3.1 through the "
                    "Gemini API (real motion, costs money per generated second). `flow` = the "
                    "Google Flow web surface driven in your signed-in Chrome (no API key, "
                    "uses your Flow subscription). `still` = one ComfyUI image per shot with "
                    "a Ken Burns move (free, local). `mock` = flat colour clips (wiring test)."
                ),
            },
            "shots": {
                "type": "array",
                "description": (
                    "Optional shot list. Each shot needs a prompt and a narration span; "
                    "supply them to take creative control instead of letting the planner decide."
                ),
                "items": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "narration": {"type": "string"},
                        "start_seconds": {"type": "number"},
                        "end_seconds": {"type": "number"},
                        "method": {
                            "type": "string",
                            "enum": ["veo", "flow", "still", "fableframe", "mock"],
                            "description": (
                                "Rendering method for this shot only. Lets one video mix "
                                "engines: Flow clips for the cinematic beats, ComfyUI stills "
                                "for the establishing shots, Fableframe for a fully animated "
                                "section. Defaults to the tool-level `provider`."
                            ),
                        },
                        "audio_path": {
                            "type": "string",
                            "description": (
                                "Optional audio for this shot only — a differently voiced "
                                "section, for example. The track is cut on the same "
                                "boundaries as the picture."
                            ),
                        },
                    },
                },
            },
            "style": {
                "type": "string",
                "default": "cinematic documentary photography",
                "description": "Visual direction applied to every shot prompt.",
            },
            "shot_seconds": {
                "type": "number",
                "default": 7.0,
                "minimum": 2.0,
                "maximum": 8.0,
                "description": "Target length of each shot when planning.",
            },
            "resolution": {
                "type": "string",
                "enum": ["720p", "1080p"],
                "default": "1080p",
            },
            "continuity": {
                "type": "string",
                "enum": ["none", "chain", "reference"],
                "default": "chain",
                "description": (
                    "How shots stay visually consistent — the mechanism Google Flow calls "
                    "'Scenes'. `chain` starts each shot from the last frame of the previous "
                    "one (seamless scene continuity). `reference` passes a fixed reference "
                    "image to every shot, so the same subject appears in each. `none` "
                    "generates every shot independently."
                ),
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Reference images (a character sheet) used with continuity=reference. "
                    "With the Veo provider these are the 'ingredients' that keep a character "
                    "consistent across shots; with the local provider they seed every image."
                ),
            },
            "fps": {"type": "integer", "default": 30},
            "crossfade_seconds": {
                "type": "number",
                "default": 0.0,
                "description": "Crossfade between shots. 0 keeps hard cuts (tightest sync).",
            },
            "burn_subtitles": {
                "type": "boolean",
                "default": False,
                "description": "Burn word-timed captions from the transcript onto the video.",
            },
            "subtitle_script": {
                "type": "string",
                "enum": ["native", "roman", "none"],
                "default": "native",
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Plan the shots and report the cost, but generate nothing.",
            },
            "work_dir": {"type": "string", "description": "Where to keep intermediate clips."},
            "keep_clips": {"type": "boolean", "default": False},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=6, ram_mb=4096, vram_mb=3072, disk_mb=4096, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout", "rate_limit"])
    idempotency_key_fields = ["audio_path", "provider", "style", "shot_seconds", "shots"]
    side_effects = [
        "writes intermediate clips and the finished MP4",
        "calls the Veo API (billed per generated second) when provider=veo",
    ]
    user_visible_verification = [
        "Watch the finished video and confirm the visuals match the narration",
    ]

    # ------------------------------------------------------------------
    # Status / cost
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        if not shutil.which("ffmpeg"):
            return ToolStatus.UNAVAILABLE
        provider = os.environ.get("LONGFORM_PROVIDER", "veo").lower()
        if provider == "veo":
            try:
                from tools.google_credentials import has_google_credentials

                if not has_google_credentials():
                    return ToolStatus.DEGRADED
            except Exception:
                return ToolStatus.DEGRADED
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        provider = str(inputs.get("provider", "veo")).lower()
        if provider != "veo":
            # Flow runs on a subscription; the local providers are free.
            return 0.0
        seconds = float(inputs.get("total_seconds") or 0.0)
        if seconds <= 0:
            audio = inputs.get("audio_path")
            if audio and Path(audio).is_file():
                seconds = _media_duration(Path(audio))
        # Veo bills the generated clip seconds, rounded up per 8s clip.
        clips = max(1, math.ceil(seconds / 8.0)) if seconds else 1
        return round(clips * 8 * 0.40, 4)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 300.0

    # ------------------------------------------------------------------
    # Transcript
    # ------------------------------------------------------------------

    def _transcribe(self, audio_path: Path) -> dict:
        """Word-timed transcript via the registry's transcriber tool.

        A transcription failure is not fatal: the tool can still plan shots
        from the audio duration and the supplied shot list. It only removes
        the option of word-level subtitles and Gemini-based shot planning.
        """
        from tools.tool_registry import registry

        try:
            registry.discover()
            tool = registry.get("transcriber")
            if tool is None:
                return {"text": "", "words": [], "error": "transcriber not registered"}
            with tempfile.TemporaryDirectory() as tmp:
                result = tool.execute({
                    "input_path": str(audio_path),
                    "output_dir": tmp,
                })
        except Exception as exc:  # noqa: BLE401 - a broken transcriber must not kill the run
            return {"text": "", "words": [], "error": f"{type(exc).__name__}: {exc}"}

        if not result.success:
            return {"text": "", "words": [], "error": result.error}
        data = result.data or {}
        words = data.get("word_timestamps") or data.get("words") or []
        return {"text": data.get("text") or "", "words": words, "error": None}

    # ------------------------------------------------------------------
    # Shot planning
    # ------------------------------------------------------------------

    def _plan_with_gemini(
        self, transcript_text: str, duration: float, style: str, shot_seconds: float
    ) -> list[dict] | None:
        """Ask Gemini for a shot list. None when no Google credentials exist."""
        try:
            from tools.google_credentials import get_genai_client

            client = get_genai_client()
        except Exception:
            return None

        target_shots = max(1, int(round(duration / max(shot_seconds, 1.0))))
        prompt = (
            "You are directing a narrated video. Split the narration into visual shots.\n\n"
            f"Visual style for every shot: {style}\n\n"
            f"Total narration duration: {duration:.1f} seconds, about {target_shots} shots.\n\n"
            "NARRATION:\n"
            f"{transcript_text}\n\n"
            "Return STRICT JSON: an array of objects, one per shot, with keys\n"
            '  "narration"  - the exact slice of narration this shot covers\n'
            '  "prompt"     - a self-contained visual prompt for a text-to-video model '
            "(describe subject, action, camera, lighting, mood; no text, no watermark)\n"
            '  "duration"   - seconds for this shot\n'
            "Cover every word of the narration exactly once, in order. No prose outside the JSON."
        )
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            if not isinstance(data, list) or not data:
                return None
            shots = []
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("narration") or "").strip()
                if not text:
                    continue
                shots.append({
                    "id": f"shot{index + 1:02d}",
                    "narration": text,
                    "prompt": str(item.get("prompt") or text),
                    "duration": float(item.get("duration") or shot_seconds),
                })
            return shots or None
        except Exception:
            return None

    def _plan_from_transcript(
        self, words: list[dict], duration: float, shot_seconds: float
    ) -> list[dict]:
        """Deterministic fallback: group words into spans of ~shot_seconds."""
        if not words:
            # No transcript available: slice the audio into even shots so the
            # video still has structure instead of one long clip.
            shots = []
            cursor = 0.0
            index = 1
            while cursor < duration - 0.2:
                span = min(shot_seconds, duration - cursor)
                shots.append({
                    "id": f"shot{index:02d}",
                    "narration": "",
                    "prompt": "",
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(cursor + span, 3),
                    "duration": round(span, 3),
                })
                cursor += span
                index += 1
            return shots

        shots: list[dict] = []
        current: list[dict] = []
        current_start = float(words[0].get("start") or 0.0)

        def flush() -> None:
            nonlocal current, current_start
            if not current:
                return
            text = " ".join(str(w.get("word") or "") for w in current).strip()
            end = float(current[-1].get("end") or current_start)
            shots.append({
                "id": f"shot{len(shots) + 1:02d}",
                "narration": text,
                "prompt": text,
                "start_seconds": round(current_start, 3),
                "end_seconds": round(end, 3),
                "duration": round(max(end - current_start, 0.1), 3),
            })
            current = []
            current_start = end

        for word in words:
            if not current:
                current_start = float(word.get("start") or 0.0)
            current.append(word)
            span = float(word.get("end") or 0.0) - current_start
            sentence_end = str(word.get("word") or "").strip()[-1:] in ".!?।"
            if span >= shot_seconds or (sentence_end and span >= MIN_SHOT_SECONDS):
                flush()
        flush()

        # Fill any gap after the last word with a closing shot.
        if shots and shots[-1]["end_seconds"] < duration - 0.4:
            last = shots[-1]
            shots.append({
                "id": f"shot{len(shots) + 1:02d}",
                "narration": "",
                "prompt": "A quiet closing shot that holds on the subject.",
                "start_seconds": last["end_seconds"],
                "end_seconds": round(duration, 3),
                "duration": round(duration - last["end_seconds"], 3),
            })
        return shots

    def _assign_spans(
        self, shots: list[dict], words: list[dict], duration: float
    ) -> list[dict]:
        """Give every shot a narration span, in order.

        The shots must cover the whole track: the join is muxed with
        ``-shortest``, so any narration the shots do not cover is silently
        cut off the end of the video. The last shot is therefore stretched to
        the end of the audio, and the first is pulled back to zero.
        """
        cursor = 0.0
        for shot in shots:
            if "start_seconds" in shot and "end_seconds" in shot:
                start = float(shot["start_seconds"])
                end = float(shot["end_seconds"])
            else:
                start = cursor
                end = start + float(shot.get("duration") or 0.0)
            end = min(end, duration)
            shot["start_seconds"] = round(start, 3)
            shot["end_seconds"] = round(end, 3)
            shot["duration"] = round(end - start, 3)
            cursor = end

        if shots:
            first = shots[0]
            if float(first.get("start_seconds") or 0.0) > 0.01:
                end = float(first["end_seconds"])
                first["start_seconds"] = 0.0
                first["end_seconds"] = round(end, 3)
                first["duration"] = round(end, 3)
            last = shots[-1]
            if float(last.get("end_seconds") or 0.0) < duration - 0.05:
                start = float(last.get("start_seconds") or 0.0)
                last["end_seconds"] = round(duration, 3)
                last["duration"] = round(max(duration - start, 0.1), 3)
        return shots

    # ------------------------------------------------------------------
    # Clip generation
    # ------------------------------------------------------------------

    def _generate_fableframe_clip(
        self,
        shot: dict,
        dest: Path,
        work: Path,
        index: int,
        audio_slice: Path | None,
        style: str = "",
    ) -> tuple[bool, str]:
        """One section rendered end to end by Fableframe.

        Fableframe owns a whole rendering method: it transcribes the narration,
        directs the scenes with a multi-agent AI director, paints visual plates
        through ComfyUI and composites the episode in Remotion. Feeding it one
        section of the narration gives that section its own look — animated SVG
        and painted plates — next to the clips other sections were made with.
        """
        from tools.tool_registry import registry

        registry.discover()
        tool = registry.get("fableframe_render")
        if tool is None:
            return False, "fableframe_render tool is not registered"
        if audio_slice is None or not audio_slice.is_file():
            return False, "fableframe needs the section's audio slice"

        # The project name carries a fingerprint of the options, so changing
        # the caption script or the mode never reuses a cached render.
        fingerprint = hashlib.sha256(
            json.dumps({
                "caption_script": shot.get("caption_script") or "native",
                "mode": shot.get("mode") or "auto",
                "style": style,
            }, sort_keys=True).encode()
        ).hexdigest()[:8]
        project_name = f"hybrid-{index:02d}-{shot.get('id', 'shot')}-{fingerprint}"
        result = tool.execute({
            "audio_path": str(audio_slice),
            "project_name": project_name,
            "output_path": str(dest),
            "mode": shot.get("mode") or "auto",
            "caption_script": shot.get("caption_script") or "native",
            "force": bool(shot.get("force")),
        })
        if not result.success:
            return False, result.error or "fableframe render failed"
        return True, ""

    def _slice_audio(self, audio: Path, start: float, duration: float, dest: Path) -> Path | None:
        """Cut a section out of the narration track."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = _run([
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(audio),
            "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(dest),
        ], timeout=300)
        if proc.returncode != 0 or not dest.is_file():
            return None
        return dest

    def _build_audio_track(
        self,
        shots: list[dict],
        global_audio: Path,
        work: Path,
    ) -> tuple[Path | None, str]:
        """Assemble one audio track from the shots.

        Each shot contributes its own audio when it has one (a differently
        voiced section), otherwise the matching slice of the global narration.
        The result is muxed onto the joined video, so the picture and the sound
        are cut on the same boundaries.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None, "ffmpeg not found"
        parts: list[Path] = []
        for index, shot in enumerate(shots):
            start = float(shot.get("start_seconds") or 0.0)
            span = float(shot.get("duration") or 0.0)
            if span <= 0.05:
                continue
            own = shot.get("audio_path")
            if own and Path(own).is_file():
                parts.append(Path(own))
                continue
            slice_path = work / "audio" / f"{index:03d}.wav"
            slice_path.parent.mkdir(parents=True, exist_ok=True)
            sliced = self._slice_audio(global_audio, start, span, slice_path)
            if sliced is not None:
                parts.append(sliced)
        if not parts:
            return None, "no audio could be assembled"
        listing = work / "audio-concat.txt"
        listing.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts), encoding="utf-8"
        )
        mixed = work / "audio-track.wav"
        proc = _run([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:a", "pcm_s16le", str(mixed),
        ], timeout=900)
        if proc.returncode != 0:
            return None, f"ffmpeg audio concat failed: {(proc.stderr or '')[-200:]}"
        return mixed, ""

    def _generate_veo_clip(
        self,
        shot: dict,
        dest: Path,
        style: str,
        resolution: str,
        generate_audio: bool,
        first_frame: Path | None = None,
        reference_images: list[str] | None = None,
    ) -> tuple[bool, str, float]:
        """One Veo clip.

        ``first_frame`` continues from the previous shot (Flow's chained
        frames); ``reference_images`` are the assets that keep a subject
        consistent across shots (Flow's Scenes).
        """
        from tools.tool_registry import registry

        registry.discover()
        tool = registry.get("veo_video")
        if tool is None:
            return False, "veo_video tool is not registered", 0.0
        prompt = f"{style}. {shot.get('prompt')}".strip(". ")

        call: dict[str, Any] = {
            "prompt": prompt,
            "duration": "8s",
            "resolution": resolution,
            "generate_audio": generate_audio,
            "output_path": str(dest),
            "backend": "auto",
        }
        if reference_images:
            call["operation"] = "reference_to_video"
            call["reference_image_paths"] = list(reference_images)
        elif first_frame and first_frame.is_file():
            call["operation"] = "image_to_video"
            call["image_path"] = str(first_frame)
        else:
            call["operation"] = "text_to_video"

        result = tool.execute(call)
        if not result.success:
            return False, result.error or "veo generation failed", 0.0
        cost = float(result.cost_usd or 0.0)
        return True, "", cost

    def _generate_flow_clip(
        self,
        shot: dict,
        dest: Path,
        style: str,
        resolution: str,
        reference_images: list[str] | None = None,
    ) -> tuple[bool, str]:
        """One clip from the Google Flow web surface.

        Flow has no API, so this drives the real page through the
        ``flow_browser`` tool. Reference images are Flow's ingredients: a
        character sheet keeps the same subject across shots.
        """
        from tools.tool_registry import registry

        registry.discover()
        tool = registry.get("flow_browser")
        if tool is None:
            return False, "flow_browser tool is not registered"
        prompt = f"{style}. {shot.get('prompt')}".strip(". ")
        result = tool.execute({
            "operation": "generate",
            "prompt": prompt,
            "reference_image_paths": list(reference_images or []),
            "output_path": str(dest),
            "aspect": "16:9",
            "resolution": resolution,
            "duration_seconds": 8,
            "headless": False,
        })
        if not result.success:
            return False, result.error or "Flow generation failed"
        return True, ""

    def _extract_last_frame(self, clip: Path, dest: Path) -> Path | None:
        """Grab the final frame of a clip, so the next shot can continue from it."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not clip.is_file():
            return None
        proc = _run([
            ffmpeg, "-y", "-sseof", "-0.2", "-i", str(clip),
            "-frames:v", "1", str(dest),
        ], timeout=300)
        if proc.returncode != 0 or not dest.is_file():
            return None
        return dest

    def _generate_still_clip(
        self,
        shot: dict,
        dest: Path,
        style: str,
        duration: float,
        resolution: str,
        fps: int,
        source_image: Path | None = None,
        stills_dir: Path | None = None,
    ) -> tuple[bool, str, Path | None]:
        """One ComfyUI image per shot, animated with a Ken Burns move.

        With ``source_image`` the image is generated *from* that frame
        (image-to-image), so the shot continues the previous one instead of
        starting a new scene.
        """
        from tools._comfyui.client import ComfyUIClient
        from tools.tool_registry import registry

        registry.discover()
        image_tool = registry.get("comfyui_image")
        if image_tool is None:
            return False, "comfyui_image tool is not registered", None

        client = ComfyUIClient(capability="image")
        models = client.list_models()
        checkpoints = models.get("checkpoints") or []
        if not checkpoints:
            return False, (
                "ComfyUI is running but exposes no checkpoints. Install one "
                "(e.g. SD 1.5) under models/checkpoints."
            ), None

        width, height = (1280, 720) if resolution == "1080p" else (960, 540)
        prompt = f"{style}. {shot.get('prompt')}".strip(". ")

        if source_image and source_image.is_file():
            workflow = json.loads(json.dumps(SD15_IMG2IMG))
            uploaded = client.upload_image(source_image, f"longform_{source_image.stem}.png")
            workflow["10"]["inputs"]["image"] = uploaded
        else:
            workflow = json.loads(json.dumps(SD15_TXT2IMG))
        workflow["3"]["inputs"]["ckpt_name"] = checkpoints[0]
        workflow["4"]["inputs"]["text"] = prompt
        if "6" in workflow:  # text-to-image carries the latent size node
            workflow["6"]["inputs"]["width"] = 768
            workflow["6"]["inputs"]["height"] = 432
        workflow["7"]["inputs"]["seed"] = abs(hash(shot.get("id", "shot"))) % (2**31)

        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / f"{shot.get('id', 'shot')}.png"
            result = image_tool.execute({
                "prompt": prompt,
                "workflow_json": json.dumps(workflow),
                "output_node": "9",
                "output_path": str(still),
            })
            if not result.success:
                return False, result.error or "comfyui image generation failed", None
            if not still.is_file():
                return False, "comfyui did not write the still image", None
            stills_dir.mkdir(parents=True, exist_ok=True)
            kept = stills_dir / f"{shot.get('id', 'shot')}.png"
            shutil.copy2(still, kept)

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return False, "ffmpeg not found", None
            frames = max(1, int(round(duration * fps)))
            # The Ken Burns move is scaled to the shot length, so a 3-second shot
            # travels as far as a 9-second one instead of sitting almost still.
            travel = 0.22 if duration <= 9 else 0.16
            rate = travel / max(frames, 1)
            vf = (
                f"scale={width * 2}:{height * 2}:flags=lanczos,"
                f"zoompan=z='min(zoom+{rate:.6f},1.0+{travel:.2f})'"
                f":d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={width}x{height}:fps={fps},"
                "format=yuv420p"
            )
            proc = _run([
                ffmpeg, "-y", "-loop", "1", "-i", str(still),
                "-t", f"{duration:.3f}", "-vf", vf,
                "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p", str(dest),
            ], timeout=600)
            if proc.returncode != 0:
                return False, f"ffmpeg failed: {(proc.stderr or '')[-300:]}", None
        return True, "", kept

    def _generate_slate_clip(
        self,
        dest: Path,
        duration: float,
        resolution: str,
        fps: int,
        label: str = "",
    ) -> tuple[bool, str]:
        """A neutral slate for a shot that could not be generated.

        Dropping a failed shot would shorten the video and desynchronise it
        from the narration. A slate of the same length keeps the cut honest:
        the hole is visible and in the right place.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        width, height = (1280, 720) if resolution == "1080p" else (960, 540)
        proc = _run([
            ffmpeg, "-y", "-f", "lavfi", "-i",
            f"color=c=0x111418:s={width}x{height}:r={fps}:d={duration:.3f}",
            "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28", "-t", f"{duration:.3f}", str(dest),
        ], timeout=300)
        if proc.returncode != 0:
            return False, f"ffmpeg slate failed: {(proc.stderr or '')[-200:]}"
        return True, ""

    def _generate_mock_clip(
        self, shot: dict, dest: Path, duration: float, resolution: str, fps: int
    ) -> tuple[bool, str]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        width, height = (1280, 720) if resolution == "1080p" else (960, 540)
        proc = _run([
            ffmpeg, "-y", "-f", "lavfi", "-i",
            f"color=c=0x1b2430:s={width}x{height}:r={fps}:d={duration:.3f}",
            "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "28", str(dest),
        ], timeout=300)
        if proc.returncode != 0:
            return False, f"ffmpeg failed: {(proc.stderr or '')[-300:]}"
        return True, ""

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def _fit_clip(
        self, clip: Path, dest: Path, span: float, fps: int, resolution: str = "1080p"
    ) -> tuple[bool, str]:
        """Trim, slow or freeze-pad a clip so it exactly covers `span` seconds.

        Every segment is re-encoded to the same codec, size, frame rate and
        pixel format, and stripped of its audio. The narration is muxed back on
        top at the end, and the join needs every segment to have the same
        stream layout — a clip that arrived with its own audio (Fableframe's
        renders do) would otherwise truncate the concatenation.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        clip_len = _media_duration(clip)
        if clip_len <= 0:
            return False, f"clip has no readable duration: {clip}"

        width, height = (1280, 720) if resolution == "1080p" else (960, 540)
        common = [
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=disable,format=yuv420p",
            "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", str(dest),
        ]

        if abs(clip_len - span) < 0.05:
            proc = _run([ffmpeg, "-y", "-i", str(clip)] + common, timeout=600)
        elif clip_len > span:
            proc = _run([
                ffmpeg, "-y", "-i", str(clip), "-t", f"{span:.3f}",
            ] + common, timeout=900)
        else:
            # Too short: slow it down if that stays natural (max 1.6x), then
            # freeze the last frame for whatever is left.
            speed = clip_len / span
            if speed >= 0.62:
                vf = f"setpts=PTS/{max(speed, 0.01):.4f},tpad=stop_mode=clone:stop_duration={span:.3f}"
            else:
                vf = f"setpts={span / clip_len:.4f}*PTS,tpad=stop_mode=clone:stop_duration={span:.3f}"
            proc = _run([
                ffmpeg, "-y", "-i", str(clip), "-t", f"{span:.3f}",
                "-vf", vf,
            ] + common, timeout=900)

        if proc.returncode != 0:
            return False, f"ffmpeg fit failed: {(proc.stderr or '')[-300:]}"
        return True, ""

    def _concat_xfade(
        self, segments: list[Path], dest: Path, crossfade: float, fps: int
    ) -> tuple[bool, str]:
        """Join with a dissolve at every boundary — without losing time.

        A plain crossfade overlaps the two shots, so the picture ends up
        shorter than the audio and the two drift apart. Each merge therefore
        holds the outgoing shot's last frame for the transition length and
        dissolves the incoming shot over that hold, so the joined length is
        exactly the sum of the shots.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        if len(segments) < 2:
            return self._concat(segments, dest, dest.parent)

        current = segments[0]
        current_len = _media_duration(current)
        work = dest.parent
        for index in range(1, len(segments)):
            nxt = segments[index]
            nxt_len = _media_duration(nxt)
            merged = work / f"merge-{index:03d}.mp4"
            proc = _run([
                ffmpeg, "-y", "-i", str(current), "-i", str(nxt),
                "-filter_complex",
                f"[0:v]tpad=stop_mode=clone:stop_duration={crossfade:.3f}[hold];"
                f"[hold][1:v]xfade=transition=fade:duration={crossfade:.3f}:"
                f"offset={current_len:.3f}[v]",
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(fps), str(merged),
            ], timeout=1800)
            if proc.returncode != 0:
                return False, f"ffmpeg dissolve failed: {(proc.stderr or '')[-300:]}"
            current = merged
            current_len += nxt_len
            try:
                merged.unlink()
            except OSError:
                pass

        shutil.move(str(current), str(dest))
        expected = sum(_media_duration(s) for s in segments)
        actual = _media_duration(dest)
        if expected > 0 and abs(actual - expected) > 0.5:
            return False, (
                f"the dissolve join is {actual:.2f}s but the shots total {expected:.2f}s"
            )
        return True, ""

    def _concat(self, segments: list[Path], dest: Path, work: Path) -> tuple[bool, str]:
        """Join the segments, then check the result really is the sum of them.

        The concat demuxer quietly stops at the first file whose stream layout
        differs, so the join is verified against the expected length instead of
        being assumed.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        listing = work / "concat.txt"
        listing.write_text(
            "\n".join(f"file '{s.resolve().as_posix()}'" for s in segments), encoding="utf-8"
        )
        proc = _run([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an", str(dest),
        ], timeout=1800)
        if proc.returncode != 0:
            return False, f"ffmpeg concat failed: {(proc.stderr or '')[-300:]}"

        expected = sum(_media_duration(s) for s in segments)
        actual = _media_duration(dest)
        if expected > 0 and actual < expected - 0.5:
            # The demuxer truncated: fall back to the filter-based join, which
            # re-encodes and tolerates differing parameters.
            inputs: list[str] = []
            for segment in segments:
                inputs += ["-i", str(segment)]
            chain = "".join(f"[{i}:v]" for i in range(len(segments)))
            filter_complex = (
                f"{chain}concat=n={len(segments)}:v=1:a=0[outv]"
            )
            proc = _run(
                [ffmpeg, "-y"] + inputs
                + ["-filter_complex", filter_complex, "-map", "[outv]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-an", str(dest)],
                timeout=1800,
            )
            if proc.returncode != 0:
                return False, f"ffmpeg concat (filter) failed: {(proc.stderr or '')[-300:]}"
            actual = _media_duration(dest)
            if actual < expected - 0.5:
                return False, (
                    f"the join is {actual:.2f}s but the segments total {expected:.2f}s"
                )
        return True, ""

    def _write_srt(self, words: list[dict], path: Path) -> bool:
        """Write readable captions from word timings.

        One subtitle per word flickers and cannot be read. Words are grouped
        into short lines — a sentence end, a pause, or roughly a dozen words —
        so the caption reads like a subtitle, not a teleprompter.
        """
        if not words:
            return False

        def stamp(value: float) -> str:
            value = max(0.0, float(value or 0.0))
            hours, rest = divmod(value, 3600)
            minutes, seconds = divmod(rest, 60)
            millis = int(round((seconds - int(seconds)) * 1000))
            return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{millis:03d}"

        max_words = 12
        max_seconds = 4.0
        min_seconds = 0.8
        gap_break = 0.45

        lines: list[str] = []
        index = 1
        group: list[dict] = []

        def flush() -> None:
            nonlocal group, index
            if not group:
                return
            text = " ".join(str(w.get("word") or "") for w in group).strip()
            start = float(group[0].get("start") or 0.0)
            end = float(group[-1].get("end") or start)
            if text:
                lines.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
                index += 1
            group = []

        for word in words:
            text = str(word.get("word") or "").strip()
            if not text:
                continue
            if group:
                previous_end = float(group[-1].get("end") or 0.0)
                start = float(word.get("start") or 0.0)
                span = float(word.get("end") or start) - float(group[0].get("start") or 0.0)
                if (
                    len(group) >= max_words
                    or span > max_seconds
                    or (start - previous_end) > gap_break
                    or text[-1] in ".!?।"
                ):
                    flush()
            group.append(word)
        flush()

        if not lines:
            return False
        path.write_text("\n".join(lines), encoding="utf-8")
        return True

    def _mux(
        self, video: Path, audio: Path, dest: Path, srt: Path | None, fps: int
    ) -> tuple[bool, str]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False, "ffmpeg not found"
        cmd = [ffmpeg, "-y", "-i", str(video), "-i", str(audio)]
        if srt and srt.is_file():
            escaped = str(srt).replace("\\", "/").replace(":", "\\:")
            cmd += ["-vf", f"subtitles='{escaped}'"]
        cmd += [
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(dest),
        ]
        proc = _run(cmd, timeout=1800)
        if proc.returncode != 0:
            return False, f"ffmpeg mux failed: {(proc.stderr or '')[-400:]}"
        return True, ""

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.time()
        audio_path = Path(inputs.get("audio_path") or "")
        output_path = Path(inputs.get("output_path") or "")
        if not audio_path.is_file():
            return ToolResult(success=False, error=f"audio_path not found: {audio_path}")
        if not output_path.name:
            return ToolResult(success=False, error="output_path is required.")
        if not shutil.which("ffmpeg"):
            return ToolResult(success=False, error="ffmpeg is required. " + self.install_instructions)

        provider = str(inputs.get("provider") or "veo").lower()
        resolution = str(inputs.get("resolution") or "1080p").lower()
        fps = int(inputs.get("fps") or 30)
        style = str(inputs.get("style") or "cinematic documentary photography")
        shot_seconds = float(inputs.get("shot_seconds") or 7.0)
        crossfade = float(inputs.get("crossfade_seconds") or 0.0)

        duration = _media_duration(audio_path)
        if duration <= 0:
            return ToolResult(success=False, error="Could not read the audio duration.")

        supplied_shots = list(inputs.get("shots") or [])
        # Transcription is only needed to plan shots and to burn word-timed
        # captions. When the shot list is supplied, skip it entirely.
        need_transcript = not supplied_shots or inputs.get("burn_subtitles")
        transcript = {"text": "", "words": [], "error": None}
        if need_transcript:
            transcript = self._transcribe(audio_path)
        words = transcript.get("words") or []

        shots = supplied_shots
        planner = "supplied"
        if not shots:
            planned = self._plan_with_gemini(
                transcript.get("text") or "", duration, style, shot_seconds
            )
            if planned:
                shots = planned
                planner = "gemini"
            else:
                shots = self._plan_from_transcript(words, duration, shot_seconds)
                planner = "transcript-split" if words else "duration-split"
        shots = self._assign_spans(shots, words, duration)

        generated_seconds = sum(max(8.0, math.ceil(s.get("duration", 0) / 8.0) * 8) for s in shots) if provider == "veo" else 0.0
        estimated_cost = round(generated_seconds * 0.40, 4) if provider == "veo" else 0.0

        plan = {
            "audio_path": str(audio_path),
            "audio_duration_seconds": round(duration, 3),
            "provider": provider,
            "planner": planner,
            "shot_count": len(shots),
            "shots": shots,
            "estimated_cost_usd": estimated_cost,
            "estimated_generated_seconds": generated_seconds,
        }

        if inputs.get("dry_run"):
            return ToolResult(
                success=True,
                data=plan,
                artifacts=[],
                cost_usd=0.0,
                duration_seconds=round(time.time() - started, 2),
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        work = Path(inputs.get("work_dir") or tempfile.mkdtemp(prefix="longform_"))
        work.mkdir(parents=True, exist_ok=True)
        clips_dir = work / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        segments: list[Path] = []
        clip_records: list[dict] = []
        total_cost = 0.0
        failures: list[str] = []
        stills_dir = work / "stills"
        continuity = str(inputs.get("continuity") or "chain").lower()
        reference_images = [
            str(p) for p in (inputs.get("reference_image_paths") or [])
            if Path(p).is_file()
        ]
        previous_still: Path | None = None
        previous_frame: Path | None = None

        for index, shot in enumerate(shots):
            span = float(shot.get("duration") or 0.0)
            if span <= 0.05:
                continue
            raw_clip = clips_dir / f"{shot.get('id', f'shot{index:02d}')}-raw.mp4"
            segment = clips_dir / f"{shot.get('id', f'shot{index:02d}')}-seg.mp4"
            # A shot may name its own rendering method, so different sections of
            # the same video can be made by different engines.
            method = str(shot.get("method") or provider).lower()

            ok, error = False, ""
            if method == "veo":
                # Flow-style continuity: a fixed reference (Scenes) or the
                # previous shot's last frame (chained frames).
                first_frame = None
                refs = reference_images
                if continuity == "chain" and previous_frame and previous_frame.is_file():
                    first_frame = previous_frame
                    refs = []
                ok, error, cost = self._generate_veo_clip(
                    shot, raw_clip, style, resolution, False,
                    first_frame=first_frame, reference_images=refs,
                )
                total_cost += cost
            elif method == "still":
                source = None
                if continuity == "reference" and reference_images:
                    source = Path(reference_images[0])
                elif continuity == "chain" and previous_still and previous_still.is_file():
                    source = previous_still
                ok, error, still = self._generate_still_clip(
                    shot, raw_clip, style, span, resolution, fps,
                    source_image=source, stills_dir=stills_dir,
                )
                if ok and still is not None:
                    previous_still = still
            elif method == "flow":
                # Flow is the web surface: ingredients keep the subject
                # consistent, and the previous shot's frame continues the scene.
                refs = list(reference_images)
                if continuity == "chain" and previous_frame and previous_frame.is_file():
                    refs = [str(previous_frame)]
                ok, error = self._generate_flow_clip(
                    shot, raw_clip, style, resolution, reference_images=refs
                )
            elif method == "fableframe":
                slice_path = self._slice_audio(
                    audio_path,
                    float(shot.get("start_seconds") or 0.0),
                    span,
                    work / "audio" / f"{index:03d}-section.wav",
                )
                ok, error = self._generate_fableframe_clip(
                    shot, raw_clip, work, index, slice_path, style
                )
            else:
                ok, error = self._generate_mock_clip(shot, raw_clip, span, resolution, fps)

            if not ok:
                failures.append(f"{shot.get('id')}: {error}")
                # Keep the section's length so the narration stays in sync; the
                # hole is shown as a slate instead of silently vanishing.
                slate = clips_dir / f"{shot.get('id', f'shot{index:02d}')}-slate.mp4"
                slate_ok, slate_error = self._generate_slate_clip(
                    slate, span, resolution, fps, label=str(shot.get("id"))
                )
                if not slate_ok:
                    failures[-1] += f" (and the placeholder failed: {slate_error})"
                    continue
                raw_clip = slate
                segment = clips_dir / f"{shot.get('id', f'shot{index:02d}')}-seg.mp4"
                fitted, fit_error = self._fit_clip(raw_clip, segment, span, fps, resolution)
                if not fitted:
                    failures[-1] += f" (placeholder not fitted: {fit_error})"
                    continue
                segments.append(segment)
                clip_records.append({
                    "id": shot.get("id"),
                    "start_seconds": shot.get("start_seconds"),
                    "end_seconds": shot.get("end_seconds"),
                    "duration_seconds": round(span, 3),
                    "narration": shot.get("narration"),
                    "prompt": shot.get("prompt"),
                    "clip": str(raw_clip),
                    "placeholder": True,
                })
                continue

            if continuity == "chain" and method in {"veo", "flow"}:
                frame = self._extract_last_frame(raw_clip, work / "frames" / f"{index:03d}.png")
                if frame is not None:
                    previous_frame = frame

            fitted, fit_error = self._fit_clip(raw_clip, segment, span, fps, resolution)
            if not fitted:
                failures.append(f"{shot.get('id')}: {fit_error}")
                continue

            segments.append(segment)
            clip_records.append({
                "id": shot.get("id"),
                "start_seconds": shot.get("start_seconds"),
                "end_seconds": shot.get("end_seconds"),
                "duration_seconds": round(span, 3),
                "narration": shot.get("narration"),
                "prompt": shot.get("prompt"),
                "clip": str(raw_clip),
            })

        if not segments:
            return ToolResult(
                success=False,
                error="No clip could be produced. " + "; ".join(failures[:5]),
            )

        silent = work / "joined.mp4"
        if crossfade > 0.05:
            ok, error = self._concat_xfade(segments, silent, crossfade, fps)
        else:
            ok, error = self._concat(segments, silent, work)
        if not ok:
            return ToolResult(success=False, error=error)

        srt_path = work / "captions.srt"
        srt_file: Path | None = None
        if inputs.get("burn_subtitles") and words:
            if self._write_srt(words, srt_path):
                srt_file = srt_path

        # The audio track is cut on the same boundaries as the picture: a shot
        # with its own audio contributes that, everything else takes its slice
        # of the global narration.
        mixed_audio, audio_error = self._build_audio_track(shots, audio_path, work)
        if mixed_audio is None:
            mixed_audio = audio_path
            audio_error = audio_error or "using the original narration track"

        ok, error = self._mux(silent, mixed_audio, output_path, srt_file, fps)
        if not ok:
            return ToolResult(success=False, error=error)

        final_duration = _media_duration(output_path)
        if not inputs.get("keep_clips"):
            shutil.rmtree(clips_dir, ignore_errors=True)

        data = {
            **plan,
            "video_path": str(output_path),
            "duration_seconds": round(final_duration, 3),
            "clips": clip_records,
            "failed_shots": failures,
            "subtitles_burned": bool(srt_file),
            "audio_track": str(mixed_audio),
            "audio_note": audio_error,
            "methods": sorted({str(s.get("method") or provider) for s in shots}),
            "complete": not failures,
            "cost_usd": round(total_cost, 4),
            "work_dir": str(work),
        }
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            cost_usd=round(total_cost, 4),
            duration_seconds=round(time.time() - started, 2),
        )
