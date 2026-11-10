"""Full video render via a local Fableframe checkout.

Fableframe owns the voice-to-video pipeline: speech-to-text, scene directing
with a multi-agent AI director, ComfyUI visual plates on the local GPU, and a
Remotion render of the finished MP4. This tool drives its CLI so an
OpenMontage pipeline can own the run ledger while Fableframe does the
rendering.

The checkout lives outside this repository and is driven through its npm
scripts (``npm run video:from-voice``), never by re-implementing its stages.

Fableframe writes the finished file to its own ``out/`` directory; this tool
copies it into the pipeline project's ``renders/`` folder so the board and the
render report point at one canonical output.
"""

from __future__ import annotations

import json
import os
import shutil
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


def _fableframe_home() -> Path | None:
    """Locate the Fableframe checkout (env var, then sibling directories)."""
    env_home = os.environ.get("FABLEFRAME_HOME")
    candidates = []
    if env_home:
        candidates.append(Path(env_home))
    candidates.extend(
        [
            REPO_ROOT.parent / "FABLEFRAME",
            REPO_ROOT.parent / "fableframe",
            REPO_ROOT.parent / "Fableframe",
            Path.home() / "FABLEFRAME",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "package.json").is_file():
            return candidate
    return None


class FableframeRender(BaseTool):
    name = "fableframe_render"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "fableframe"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:npm"]
    install_instructions = (
        "Install Fableframe next to this repository and install its dependencies:\n"
        "  cd FABLEFRAME && npm install\n"
        "Then point at it with FABLEFRAME_HOME=/path/to/FABLEFRAME if it lives "
        "somewhere else."
    )

    agent_skills = ["remotion", "remotion-best-practices"]

    capabilities = [
        "voice_to_video",
        "scene_direction",
        "local_diffusion_plates",
        "remotion_render",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": True,
        "native_audio": True,
    }
    best_for = [
        "narrated explainers and story shorts generated end to end on one GPU",
        "Hindi or other Indic narration with native-script captions",
    ]
    not_good_for = [
        "clip generation from a text prompt alone (needs a narration track)",
        "video with real footage (use the footage-led pipelines)",
    ]

    input_schema = {
        "type": "object",
        "required": ["audio_path"],
        "properties": {
            "audio_path": {
                "type": "string",
                "description": "Narration audio to build the video from (wav/mp3/m4a/flac/ogg).",
            },
            "project_name": {
                "type": "string",
                "description": "Slug for the Fableframe project folder. Defaults to a timestamped name.",
            },
            "output_path": {
                "type": "string",
                "description": "Where to place the finished MP4 (usually <project>/renders/final.mp4).",
            },
            "mode": {
                "type": "string",
                "enum": ["story", "song", "auto"],
                "default": "auto",
            },
            "caption_script": {
                "type": "string",
                "enum": ["native", "roman", "none"],
                "default": "native",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "description": "Re-run every stage instead of reusing cached artifacts.",
            },
            "timeout_seconds": {"type": "integer", "default": 3600},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=6, ram_mb=4096, vram_mb=3072, disk_mb=2048, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["audio_path", "project_name", "mode"]
    side_effects = [
        "writes a project folder into FABLEFRAME/videos/<name>/",
        "writes the finished MP4 to the requested output_path",
    ]
    user_visible_verification = [
        "Watch the rendered MP4 and confirm the narration matches the visuals",
    ]

    def get_status(self) -> ToolStatus:
        home = _fableframe_home()
        if home is None:
            return ToolStatus.UNAVAILABLE
        if not (home / "node_modules").is_dir():
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 240.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error="Fableframe checkout or its node_modules not found. "
                + self.install_instructions,
            )

        home = _fableframe_home()
        audio_path = Path(inputs.get("audio_path") or "")
        if not audio_path.is_file():
            return ToolResult(success=False, error=f"audio_path not found: {audio_path}")

        project_name = inputs.get("project_name") or f"om-{int(time.time())}"
        output_path = inputs.get("output_path")

        node = shutil.which("node")
        if not node:
            return ToolResult(success=False, error="node not found on PATH.")

        # Fableframe's own npm script is `node --env-file-if-exists=.env
        # scripts/video-from-voice.mjs`. Calling node directly keeps the .env
        # loading and skips the npm wrapper (which is a .cmd on Windows and
        # cannot be exec'd directly by subprocess).
        command = [
            node,
            "--env-file-if-exists=.env",
            "scripts/video-from-voice.mjs",
            "--audio",
            str(audio_path),
            "--name",
            project_name,
            "--mode",
            inputs.get("mode", "auto"),
            "--caption-script",
            inputs.get("caption_script", "native"),
            "--render",
        ]
        if inputs.get("force"):
            command.append("--force")

        started = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=str(home),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(inputs.get("timeout_seconds") or 3600),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Fableframe render timed out.")

        if proc.returncode != 0:
            tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-800:]
            return ToolResult(
                success=False,
                error=f"Fableframe exited with {proc.returncode}: {tail}",
            )

        rendered = [
            home / "out" / f"{project_name}.mp4",
            home / "videos" / project_name / "out" / f"{project_name}.mp4",
        ]
        source = next((p for p in rendered if p.is_file()), None)
        if source is None:
            candidates = list((home / "out").glob(f"{project_name}*.mp4"))
            source = candidates[0] if candidates else None
        if source is None:
            return ToolResult(
                success=False,
                error="Fableframe reported success but no MP4 was found.",
            )

        final_path = Path(output_path) if output_path else source
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != final_path.resolve():
            shutil.copy2(source, final_path)

        project_dir = home / "videos" / project_name
        episode: dict[str, Any] = {}
        episode_file = project_dir / "episode.json"
        if episode_file.is_file():
            try:
                episode = json.loads(episode_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                episode = {}

        scenes = episode.get("scenes") or []
        assets = [
            str(p)
            for p in sorted((project_dir / "assets").glob("*.png"))
        ]

        return ToolResult(
            success=True,
            data={
                "video_path": str(final_path),
                "source_path": str(source),
                "project_dir": str(project_dir),
                "scene_count": len(scenes),
                "backgrounds": sorted({s.get("background") for s in scenes if s.get("background")}),
                "duration_seconds": round(
                    sum(float(s.get("durationSec") or 0) for s in scenes), 3
                ),
                "episode_title": episode.get("title"),
                "platform_assets": assets,
                "render_engine": "remotion",
                "stdout_tail": (proc.stdout or "")[-800:],
            },
            artifacts=[str(final_path)] + assets,
            cost_usd=0.0,
            duration_seconds=round(time.time() - started, 2),
        )
