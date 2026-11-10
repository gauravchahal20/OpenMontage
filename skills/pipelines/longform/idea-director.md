# Idea Director — Google Longform Pipeline

## When To Use

You are turning a finished narration track into a production plan. This pipeline
starts from **audio that already exists** — a voiceover recording, a TTS render —
so the brief is about illustration, not about narration.

Before you plan anything, settle the two choices that cost money: the **clip provider**
(Veo versus the free local one) and the **composition runtime**. This pipeline joins the
generated shots with FFmpeg; if the piece instead needs OpenMontage's stock scene types,
that is Remotion, and HyperFrames (the `hyperframes` runtime, HTML/CSS/GSAP) is the pick for typographic
motion. **Present both** runtime options to the user, recommend one with a reason, and log
the choice as a `render_runtime_selection` decision before locking it in.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/brief.schema.json` | Artifact validation |
| Prior artifacts | none | — |
| Tooling | none at this stage | — |

## Process

### 1. Find the narration

Locate the audio file and read its duration. Everything downstream is measured
against it. If the user has no audio yet, this is the wrong pipeline — send them
to `fableframe-studio` (which synthesizes the voice) or to `podcast-repurpose`.

### 2. Name the clip provider, and its price

This is the decision that matters here, because hosted video models generate
short clips and the bill follows **generated seconds, not runtime**:

| Provider | Clip | Cost | Needs |
|---|---|---|---|
| `veo` | 8s at 1080p (Veo 3.1) | ~$0.40 / generated second → ~$3.20 per 8s clip | Google key (`GOOGLE_API_KEY` / Vertex credentials) |
| `still` | one ComfyUI image per shot + Ken Burns move | free, local | ComfyUI running on 8188 |
| `mock` | flat colour clip | free | nothing |

Work out the number of shots (narration duration ÷ ~7s) and multiply it out.
A 60-second narration is ~9 shots: about **$29 with Veo**, **$0 with the local
provider**. State the number and the provider before anything is generated.

### 3. Name the composition runtime

The clips are joined by the `longform_video` assembler (FFmpeg), not by a
scene-template renderer. If the piece instead needs OpenMontage's stock scene
types — stat cards, charts, terminal scenes — say so now and hand it to
`animated-explainer`, whose `render_runtime` is Remotion. HyperFrames (the
HTML/CSS/GSAP runtime) is the right pick only for typographic motion. Either
way, record the choice as a `render_runtime_selection` decision in
`decision_log` with **both** options listed — do not pick one silently.

### 4. Write the brief

Fill the schema, and record the production facts in `metadata`:

```json
{
  "metadata": {
    "narration_path": "projects/<id>/assets/audio/narration.wav",
    "narration_duration_seconds": 74.2,
    "clip_provider": "veo",
    "shot_budget": 11,
    "estimated_cost_usd": 35.2,
    "aspect": "16:9",
    "resolution": "1080p",
    "burn_subtitles": true
  }
}
```

`style` must be something a video model can act on: "warm documentary
photography, golden hour, shallow depth of field" — not "nice".

## Quality Bar

- The narration file is named and its duration is known.
- The provider and the resulting cost are stated in plain language.
- The visual style is specific enough that two shots would look like the same film.

## Output

Write `brief` to `projects/<project-id>/artifacts/brief.json`, validate it against
the schema, then checkpoint.

## Anti-Patterns

- Quoting a price per finished second — the bill is per *generated* second, and
  a 60-second video costs more than 60 seconds of Veo.
- Starting without confirming the audio exists.
- Choosing the style as an adjective ("beautiful") instead of a description.
