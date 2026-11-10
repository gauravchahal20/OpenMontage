# Asset Director — Google Longform Pipeline

## When To Use

You are generating one clip per shot. This is the stage that spends money, so the
plan and the cost come first.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["scene_plan"]["scene_plan"]`, `state.artifacts["script"]["script"]` | Shots and the narration they cover |
| Tool | `longform_video` | Plans, generates, fits and joins the shots |
| Layer 3 skill | `.agents/skills/ai-video-gen/SKILL.md` | Read before writing video prompts |

## Process

### 1. Announce the provider and the bill before the first call

```
Tool: longform_video | Provider: Google Veo 3.1 | Model: veo-3.1
Cost: 8 shots x 8s x $0.40 = $25.60 (billed on generated seconds, not runtime)
Runtime: ~6 minutes for 8 clips
```

Or, for the free local route:

```
Tool: longform_video | Provider: local still (ComfyUI SD 1.5 + Ken Burns)
Cost: $0.00 | Runtime: ~3 minutes on the RTX 3050
```

Or, for the Google Flow web surface (no API key — it drives the signed-in browser,
and the clip comes out of the user's Flow subscription):

```
Tool: longform_video | Provider: Google Flow web surface (flow_browser)
Cost: $0.00 (uses the Flow subscription) | Runtime: ~10 minutes, browser visible
```

The Flow route needs a signed-in session: run `flow_browser` with
`operation: "login"` once before the first generation.

### 2. Dry-run first

Always plan before generating:

```python
from tools.tool_registry import registry
registry.discover()
plan = registry.get("longform_video").execute({
    "audio_path": "<project>/assets/audio/narration.wav",
    "output_path": "<project>/renders/final.mp4",
    "provider": "veo",
    "dry_run": True,
})
```

The dry run returns the shot list, the planner that produced it (`gemini`,
`transcript-split` or `supplied`) and the estimated cost — with **no** API calls.
Read the shot list before approving the spend: check that the prompts match the
narration and that the count matches the budget.

### 3. Generate

```python
result = registry.get("longform_video").execute({
    "audio_path": "<project>/assets/audio/narration.wav",
    "output_path": "<project>/renders/final.mp4",
    "provider": "veo",
    "style": "warm documentary photography, golden hour, shallow depth of field",
    "shot_seconds": 7.0,
    "burn_subtitles": True,
})
```

The tool does the whole chain: transcribe → plan shots → one clip per shot →
fit each clip to its narration span (trim, slow or freeze-pad) → join → mux the
original narration on top.

**Your audio is the audio that ships.** The tool never regenerates the voice.

### 4. Check the result before checkpointing

- `clips` — one entry per shot, each with its span.
- `failed_shots` — anything that failed to generate is listed here. A failed shot
  is a hole in the video; regenerate it rather than shipping the gap.
- `cost_usd` — what was actually spent, against the estimate.

Watch at least the first shot, the middle and the last one. A wrong subject or a
frozen frame is cheaper to catch now than after the compose stage.

## Quality Bar

- One clip per shot, each the right length.
- Shots are visually consistent with each other.
- The cost is within the approved budget.

## Output

Write `asset_manifest` to `projects/<project-id>/artifacts/asset_manifest.json`
with one entry per clip (type `video`, path, source tool, scene id, duration,
cost), validate against the schema, then checkpoint.

## Anti-Patterns

- Generating without a dry run — the shot list is the contract with the bill.
- Re-running the whole generation to fix one bad shot.
- Reporting success while `failed_shots` is non-empty.
