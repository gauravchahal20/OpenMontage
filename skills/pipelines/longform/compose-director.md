# Compose Director — Google Longform Pipeline

## When To Use

You are delivering the finished video. The assembly itself happens inside
`longform_video` — it fits every clip to its narration span, joins the shots and
muxes the narration back on top — so this stage is about verification and
delivery, not about re-cutting.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/render_report.schema.json`, `schemas/artifacts/final_review.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["assets"]["asset_manifest"]`, `state.artifacts["scene_plan"]["scene_plan"]` | Clips and their intended spans |
| Tool | `longform_video` | The assembler |
| Layer 3 skill | `.agents/skills/ffmpeg/SKILL.md` | For anything that needs a second pass |

## Process

### 1. Name the runtime

This pipeline's composition runtime is the FFmpeg assembler inside
`longform_video` — clips are joined and the narration is muxed, not re-templated.
Record it as a `render_runtime_selection` decision in `decision_log`:

| Runtime | Fits |
|---|---|
| `ffmpeg` (this pipeline) | joining generated shots onto an existing narration track |
| `remotion` | OpenMontage's scene-type compositions, charts, stat cards |
| `hyperframes` | HTML/CSS/GSAP kinetic typography |

If the brief needs one of the others, say so before rendering and log the
decision with **both** options listed. Never swap the runtime silently.

### 2. Render

```python
from tools.tool_registry import registry
registry.discover()
result = registry.get("longform_video").execute({
    "audio_path": "<project>/assets/audio/narration.wav",
    "output_path": "<project>/renders/final.mp4",
    "provider": "veo",
    "style": "<the approved style>",
    "shot_seconds": 7.0,
    "burn_subtitles": True,
    "keep_clips": True,
})
```

`keep_clips: True` keeps the per-shot clips in the work directory, which is what
you need if a single shot has to be regenerated.

### 3. Verify the file, then watch it

Run the checker first — it measures what a render log cannot:

```bash
.venv\\Scripts\\python.exe scripts\\quality_check_video.py renders\\final.mp4 \
    --narration assets\\audio\\narration.wav --out renders\\final.quality.md
```

It reports the streams, the duration against the narration, the audio level, black
frames, repeated frames and the amount of motion between frames, and scores the
result out of 10. It exits non-zero below `--min-score` (default 7), so it can
gate the stage.

Then watch it, because the checker cannot see these four failures:

1. **A frozen frame** — a clip shorter than its span was freeze-padded instead
   of slowed. Visible as a still image held for seconds.
2. **A shot that does not match its words** — the prompt and the narration drifted.
3. **A repeated shot** — two clips came back near-identical.
4. **Captions out of sync** — the burn used different timings than the transcript.

### 4. Write the report

```json
{
  "version": "1.0",
  "outputs": [{
    "path": "renders/final.mp4",
    "format": "mp4",
    "codec": "h264",
    "audio_codec": "aac",
    "resolution": "1920x1080",
    "fps": 30,
    "duration_seconds": 52.4,
    "file_size_bytes": 48234496,
    "platform_target": "youtube"
  }],
  "render_time_seconds": 214.6,
  "render_grammar": "animation-first",
  "verification_notes": ["ffprobe: 1920x1080 @ 30/1, h264 + aac, 52.42s", "8 shots joined"],
  "warnings": []
}
```

Then `final_review` with what you actually watched, including the defects you
found. A report that claims more than the reviewer checked is a contract
violation.

## Quality Bar

- The MP4 exists, passes ffprobe, and has both streams.
- The duration matches the narration.
- Every shot matches its words and no frame is frozen.

## Anti-Patterns

- Shipping a video with holes where `failed_shots` was non-empty.
- Reporting "looks good" without watching the file.
- Re-running the whole generation to fix one shot — regenerate that shot.
