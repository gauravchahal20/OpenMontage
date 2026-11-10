"""Local neural text-to-speech via OmniVoice.

OmniVoice is a zero-shot TTS model (600+ languages, voice cloning, voice
design) that runs on the local GPU. Its checkout lives outside this
repository and ships its own virtualenv — the ``omnivoice`` package and its
CUDA-enabled torch build are not installable into this repo's venv without a
second copy of torch — so this tool drives that interpreter directly.

Two generation modes:

* **voice design** — pass ``instruct`` ("Male, Middle-aged, Indian Accent")
  and the model synthesises a voice matching those attributes.
* **voice cloning** — pass ``ref_audio`` (3-10s clip) plus ``ref_text``.

A script is one call: give ``lines`` and the model is loaded once and reused
for every line, because loading costs ~20s and would otherwise dominate the
runtime. The per-line WAVs are kept and the lines are concatenated (with an
optional gap) into a single narration track.

Everything runs offline on the local GPU. Cost is zero.
"""

from __future__ import annotations

import json
import os
import subprocess
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

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_SCRIPT = Path(__file__).resolve().parent.parent / "_omnivoice" / "batch_tts.py"


def _omnivoice_home() -> Path | None:
    """Locate the OmniVoice checkout.

    Order: ``OMNIVOICE_HOME`` env var, then the usual sibling locations next
    to this repository (the three projects are kept side by side).
    """
    env_home = os.environ.get("OMNIVOICE_HOME")
    candidates = []
    if env_home:
        candidates.append(Path(env_home))
    candidates.extend(
        [
            REPO_ROOT.parent / "OmniVoice",
            REPO_ROOT.parent / "omnivoice",
            REPO_ROOT.parent / "OMNIVOICE",
            Path.home() / "OmniVoice",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "omnivoice").is_dir():
            return candidate
    return None


def _venv_python(home: Path) -> Path | None:
    for relative in (Path(".venv/Scripts/python.exe"), Path(".venv/bin/python")):
        candidate = home / relative
        if candidate.is_file():
            return candidate
    return None


class OmniVoiceTTS(BaseTool):
    name = "omnivoice_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "omnivoice"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL_GPU

    dependencies = []
    install_instructions = (
        "Install OmniVoice next to this repository (it keeps its own venv):\n"
        "  git clone https://github.com/k2-fsa/OmniVoice.git\n"
        "  cd OmniVoice && python -m venv .venv\n"
        "  .venv\\Scripts\\pip install -r requirements.txt\n"
        "Then point at it with OMNIVOICE_HOME=/path/to/OmniVoice if it lives "
        "somewhere else."
    )

    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_design",
        "voice_cloning",
        "multilingual",
        "offline_generation",
    ]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": True,
        "native_audio": True,
    }
    best_for = [
        "free offline narration in 600+ languages",
        "distinct character voices via attribute instructions",
        "multi-voice dialogue from one local model",
    ]
    not_good_for = [
        "zero-shot voice cloning from a very short sample (<3s)",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Single narration line. Use `lines` for a script.",
            },
            "lines": {
                "type": "array",
                "description": "Script lines to synthesize; the model loads once for all of them.",
                "items": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "instruct": {
                            "type": "string",
                            "description": "Voice attributes, e.g. 'Female, Young adult, High pitch'.",
                        },
                        "language": {"type": "string"},
                        "speed": {"type": "number"},
                    },
                },
            },
            "instruct": {
                "type": "string",
                "description": "Voice attributes for the single-line form, e.g. 'Male, Middle-aged, Indian Accent'.",
            },
            "language": {"type": "string", "description": "Language name; omit to let the model decide."},
            "speed": {"type": "number", "default": 1.0},
            "num_step": {"type": "integer", "default": 16, "minimum": 4, "maximum": 64},
            "gap_seconds": {
                "type": "number",
                "default": 0.34,
                "description": "Silence inserted between concatenated lines.",
            },
            "output_path": {
                "type": "string",
                "description": "Combined narration WAV. Per-line WAVs are written next to it in `segments/`.",
            },
            "segments_dir": {"type": "string", "description": "Directory for per-line WAVs."},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=4096, vram_mb=3072, disk_mb=4096, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout"])
    idempotency_key_fields = ["text", "lines", "instruct", "num_step"]
    side_effects = ["writes WAV files to output_path and segments_dir"]
    user_visible_verification = [
        "Listen to the narration for intelligibility and correct language",
    ]

    def get_status(self) -> ToolStatus:
        home = _omnivoice_home()
        if home is None or _venv_python(home) is None:
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        lines = inputs.get("lines") or ([{"text": inputs.get("text", "")}] if inputs.get("text") else [])
        return 30.0 + len(lines) * 12.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error="OmniVoice checkout not found. " + self.install_instructions,
            )

        home = _omnivoice_home()
        python = _venv_python(home)  # type: ignore[arg-type]

        lines = list(inputs.get("lines") or [])
        if not lines and (inputs.get("text") or "").strip():
            lines = [{"id": "narration", "text": inputs["text"]}]
        if not lines:
            return ToolResult(success=False, error="No text to synthesize (pass `text` or `lines`).")

        output_path = inputs.get("output_path")
        if not output_path:
            return ToolResult(success=False, error="output_path is required.")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        segments_dir = Path(inputs.get("segments_dir") or (output.parent / "segments"))
        segments_dir.mkdir(parents=True, exist_ok=True)

        job_lines = []
        for index, line in enumerate(lines):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            segment_id = line.get("id") or f"seg{index + 1:02d}"
            job_lines.append(
                {
                    "id": segment_id,
                    "text": text,
                    "instruct": line.get("instruct") or inputs.get("instruct") or "",
                    "language": line.get("language") or inputs.get("language") or None,
                    "speed": line.get("speed", inputs.get("speed", 1.0)),
                    "output": str(segments_dir / f"{segment_id}.wav"),
                }
            )

        job = {
            "lines": job_lines,
            "num_step": int(inputs.get("num_step") or 16),
            "combine_output": str(output),
            "gap_seconds": float(inputs.get("gap_seconds", 0.34)),
        }

        env = {
            **os.environ,
            "OMNIVOICE_HOME": str(home),
            "PYTHONPATH": str(home) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }

        started = time.time()
        try:
            proc = subprocess.run(
                [str(python), str(BRIDGE_SCRIPT)],
                input=json.dumps(job),
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(home),
                env=env,
                timeout=int(inputs.get("timeout_seconds") or 1800),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="OmniVoice generation timed out.")

        payload = None
        for raw in (proc.stdout or "").splitlines():
            if raw.startswith("__OMNIVOICE_RESULT__"):
                payload = json.loads(raw[len("__OMNIVOICE_RESULT__"):])
                break

        if payload is None:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            return ToolResult(
                success=False,
                error=f"OmniVoice bridge produced no result (exit {proc.returncode}): {tail}",
            )
        if not payload.get("ok"):
            return ToolResult(success=False, error=payload.get("error") or "OmniVoice generation failed.")

        artifacts = [str(output)] + [
            seg["path"] for seg in payload.get("segments", []) if seg.get("path")
        ]
        return ToolResult(
            success=True,
            data={
                "audio_path": str(output),
                "duration_seconds": payload.get("combined_duration_seconds"),
                "segments": payload.get("segments", []),
                "device": payload.get("device"),
                "model": payload.get("model"),
                "sample_rate": payload.get("sample_rate"),
                "load_seconds": payload.get("load_seconds"),
                "generation_seconds": payload.get("generation_seconds"),
            },
            artifacts=artifacts,
            cost_usd=0.0,
            duration_seconds=round(time.time() - started, 2),
            model=payload.get("model"),
        )
