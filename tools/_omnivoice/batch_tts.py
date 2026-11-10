"""Batch TTS bridge for OmniVoice.

This script is executed by OmniVoice's own virtualenv interpreter, not by this
repository's venv: the ``omnivoice`` package and its CUDA-enabled torch build
live there. It reads one JSON job on stdin and writes one JSON result on
stdout, so the caller never has to parse progress bars or log noise.

Job (stdin)::

    {
      "lines": [
        {"id": "seg01",
         "text": "narration text",
         "instruct": "Male, Middle-aged, Indian Accent",   # optional (voice design)
         "language": null,                                   # optional
         "speed": 1.0,                                      # optional
         "output": "C:/abs/path/seg01.wav"}                 # optional
        }
      ],
      "num_step": 16,
      "combine_output": "C:/abs/path/narration.wav",  # optional
      "gap_seconds": 0.34                           # optional
    }

Result (stdout)::

    {"ok": true,
     "segments": [{"id": "seg01", "path": "...", "duration_seconds": 3.2}],
     "combined_path": "...", "combined_duration_seconds": 12.5,
     "device": "cuda", "model": "...", "generation_seconds": 9.4}

The model is loaded once and reused for every line — loading costs ~20s, so
one process per line would dominate the runtime of a multi-line script.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _emit(payload: dict) -> None:
    """Write the single JSON result line to stdout."""
    sys.stdout.write("\n__OMNIVOICE_RESULT__" + json.dumps(payload) + "\n")
    sys.stdout.flush()


def _resolve_weights(model_arg: str | None) -> str:
    """Prefer locally downloaded weights over the Hugging Face cache."""
    if model_arg:
        return model_arg
    home = Path(os.environ.get("OMNIVOICE_HOME", Path.cwd()))
    local = home / "models" / "OmniVoice"
    if (local / "model.safetensors").is_file() and (
        local / "audio_tokenizer" / "model.safetensors"
    ).is_file():
        return str(local)
    return "k2-fsa/OmniVoice"


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": f"invalid job JSON: {exc}"})
        return 2

    lines = job.get("lines") or []
    if not lines:
        _emit({"ok": False, "error": "job contained no lines"})
        return 2

    import numpy as np
    import soundfile as sf
    import torch
    from omnivoice import OmniVoice, OmniVoiceGenerationConfig

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    weights = _resolve_weights(job.get("model"))
    num_step = int(job.get("num_step") or 16)

    t0 = time.perf_counter()
    model = OmniVoice.from_pretrained(
        weights, device_map=device, dtype=dtype, load_asr=False
    )
    load_seconds = time.perf_counter() - t0

    sample_rate = model.sampling_rate
    segments: list[dict] = []
    chunks: list = []
    generation_seconds = 0.0

    for index, line in enumerate(lines):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        cfg = OmniVoiceGenerationConfig(num_step=num_step)
        kwargs = {
            "text": text,
            "language": line.get("language") or None,
            "generation_config": cfg,
        }
        speed = line.get("speed")
        if speed is not None and abs(float(speed) - 1.0) > 1e-6:
            kwargs["speed"] = float(speed)
        instruct = (line.get("instruct") or "").strip()
        if instruct:
            kwargs["instruct"] = instruct

        started = time.perf_counter()
        try:
            audios = model.generate(**kwargs)
        except Exception as exc:  # noqa: BLE001 - report the real error upstream
            _emit(
                {
                    "ok": False,
                    "error": f"generation failed for segment {index}: "
                    f"{type(exc).__name__}: {exc}",
                }
            )
            return 1
        generation_seconds += time.perf_counter() - started

        audio = np.asarray(audios[0], dtype=np.float32)
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak > 0.99:
            audio = audio / peak * 0.9

        out_path = line.get("output")
        if out_path:
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(target), audio, sample_rate)

        segments.append(
            {
                "id": line.get("id") or f"seg{index + 1:02d}",
                "path": out_path,
                "duration_seconds": round(len(audio) / sample_rate, 3),
            }
        )
        chunks.append(audio)

    combined_path = job.get("combine_output")
    combined_duration = 0.0
    if chunks and combined_path:
        gap = float(job.get("gap_seconds") or 0.0)
        silence = np.zeros(int(gap * sample_rate), dtype=np.float32) if gap > 0 else None
        merged: list = []
        for chunk in chunks:
            if merged and silence is not None:
                merged.append(silence)
            merged.append(chunk)
        joined = np.concatenate(merged)
        target = Path(combined_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(target), joined, sample_rate)
        combined_duration = round(len(joined) / sample_rate, 3)

    _emit(
        {
            "ok": True,
            "segments": segments,
            "combined_path": combined_path if combined_path else None,
            "combined_duration_seconds": combined_duration,
            "device": device,
            "dtype": str(dtype),
            "model": weights,
            "sample_rate": sample_rate,
            "num_step": num_step,
            "load_seconds": round(load_seconds, 2),
            "generation_seconds": round(generation_seconds, 2),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
