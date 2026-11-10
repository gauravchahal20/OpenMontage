# Scene Director — Google Longform Pipeline

## When To Use

You are turning the timed narration into a shot list. This is the stage where
"long video" stops being a wish and becomes a sequence of clips that a video
model can actually produce.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/scene_plan.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["script"]["script"]`, `state.artifacts["idea"]["brief"]` | Timed narration, style, provider |
| Layer 3 skill | `.agents/skills/ai-video-gen/SKILL.md` | Read before writing video prompts |

## Process

### 1. Respect the clip limit

| Provider | Max clip | Cost |
|---|---|---|
| Veo 3.1 | **8s at 1080p** (4s/6s only at 720p) | ~$0.40 per generated second |
| local still | any length (the still is animated, not generated) | free |

A narration span longer than the clip limit becomes **more than one shot** —
never one stretched clip. Split it: "the river at night, the boy's face lit by
the glow" instead of one 12-second prompt.

### 2. One shot per narration beat

For each section of the script, decide what the viewer sees while those words
are spoken. Write a prompt that stands on its own:

> subject + action + camera + light + mood

Not: *"something about the moon"* — but *"a teenage boy in a white kurta stands
at the water's edge, seen from behind, moonlit river, low angle, cool blue
light, quiet wonder"*.

No on-screen text, no logos, no watermarks — video models render text badly.

### 3. Plan continuity

Independent clips drift. Keep them in the same film by repeating in every
prompt:

- the recurring subject's appearance (age, clothing, colour),
- the palette and light ("golden hour", "cold blue night"),
- the aspect and lens language ("35mm, shallow depth of field").

When the provider supports it, anchor a shot to the previous one — Veo can take a
reference image or a first/last frame. That is the difference between a sequence
and a montage.

### 4. Write the scene plan

One entry per shot with the narration span it covers, the prompt, and the
planned duration. Cover the whole narration: no gaps, no overlaps.

## Quality Bar

- Every shot maps to a real span, and the spans cover the track.
- No prompt exceeds the provider's clip limit.
- Two adjacent prompts would look like the same film if played side by side.

## Output

Write `scene_plan` to `projects/<project-id>/artifacts/scene_plan.json`, validate
against the schema, then checkpoint.

## Anti-Patterns

- One shot for a long stretch of narration.
- Prompts that describe emotion without describing anything visible.
- Ignoring the clip limit and discovering it when the bill arrives.
