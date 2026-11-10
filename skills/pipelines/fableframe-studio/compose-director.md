# Compose Director — Fableframe Studio Pipeline

## When To Use

You are producing the finished video. In this pipeline the composition engine is **Fableframe**: it re-transcribes the narration, directs scenes with a multi-agent AI director, paints a visual plate per scene through ComfyUI, and renders the MP4 with Remotion.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/render_report.schema.json`, `schemas/artifacts/final_review.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["assets"]["asset_manifest"]`, `state.artifacts["script"]["script"]` | Narration track and intent |
| Tool | `fableframe_render` | Drives the Fableframe CLI |
| Layer 3 skills | `.agents/skills/remotion/SKILL.md`, `.agents/skills/remotion-best-practices/SKILL.md` | Composition rules |

## Process

### 1. Name the runtime before rendering

This pipeline renders through Fableframe's Remotion engine. State it before the call, and record the choice as a `render_runtime_selection` decision in `decision_log`:

```
Render runtime: Remotion (inside Fableframe) | Composition: SvgEpisode
Canvas: 1280x720 @ 30fps | Codec: h264 + aac | Cost: $0.00
```

OpenMontage's own `video_compose` (Remotion or HyperFrames) is the alternative when the piece needs OpenMontage's stock scene types — stat cards, bar charts, terminal scenes — rather than painted narrative plates. Say which is being used and why; do not silently substitute one for the other.

**Route by `render_runtime`.** This pipeline's manifest locks `render_runtime` to Remotion (inside Fableframe), because the visuals are painted from the narration rather than assembled from stock scene types. HyperFrames is the alternative runtime for HTML/CSS/GSAP composition; it is installed on this machine and is the right pick when the brief calls for kinetic typography rather than narrative plates. If the user wants HyperFrames, present both options, log a `render_runtime_selection` decision with **both** runtimes in `options_considered`, and only then render. A silent runtime swap is a governance violation.

### 2. Render

```python
from tools.tool_registry import registry
registry.discover()
result = registry.get("fableframe_render").execute({
    "audio_path": "<project>/assets/audio/narration.wav",
    "project_name": "<project-id>",
    "output_path": "<project>/renders/final.mp4",
    "mode": "auto",          # story | song | auto
    "caption_script": "native",
})
```

`mode="auto"` lets Fableframe classify the content (story, explainer, song) from the transcript. Use `story` when the script is a narrative and `song` when it is a lyric. `caption_script="native"` renders Devanagari captions for Hindi; `roman` transliterates.

The render is long — expect 2–6 minutes for a one-minute video on a 4GB GPU. Set `timeout_seconds` accordingly.

### 3. Verify the output

Do not trust the exit code alone. Probe the file:

- resolution 1280x720, fps 30/1,
- a video stream (h264) **and** an audio stream (aac),
- duration within ±5% of the narration track,
- file size in the expected range (a minute of 720p with audio is roughly 10–20MB).

Then watch it. The three things that actually go wrong:

1. **Repeated plates** — the same image across scenes (the ComfyUI prompt cache keyed too coarsely).
2. **Captions out of sync** — the narration was edited after rendering, or the script and audio disagree.
3. **Silence** — the audio track did not attach.

### 4. Check the scene plan

The tool returns `scene_count`, `backgrounds` and `duration_seconds` from Fableframe's `episode.json`. If every scene shares one background, the piece will feel like a slideshow — that is a direction problem from the script stage, not a render problem. Report it rather than re-rendering.

### 5. Write the report

```json
{
  "version": "1.0",
  "outputs": [{
    "path": "renders/final.mp4",
    "format": "mp4",
    "codec": "h264",
    "audio_codec": "aac",
    "resolution": "1280x720",
    "fps": 30,
    "duration_seconds": 41.4,
    "file_size_bytes": 15482912,
    "platform_target": "youtube"
  }],
  "render_time_seconds": 168.4,
  "render_grammar": "animation-first",
  "verification_notes": ["ffprobe: 1280x720 @ 30/1, h264 + aac, 41.40s"],
  "warnings": []
}
```

Then write `final_review` with what you actually saw, including anything that looked wrong. A render report that claims more than the reviewer checked is a contract violation.

## Quality Bar

- The MP4 exists in `renders/`, passes ffprobe, and has both streams.
- The narration is audible and in sync with the visuals.
- Scenes are visually distinct.

## Anti-Patterns

- Rendering without a `final_review` that says what was wrong.
- Re-rendering to fix a direction problem (repeated backgrounds, wrong tone) — that is a script/brief revision, and it needs a new checkpoint.
- Swapping the render runtime without logging the change in `decision_log`.
