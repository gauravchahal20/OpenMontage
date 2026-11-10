# Idea Director — Fableframe Studio Pipeline

## When To Use

You are turning a rough topic into a brief that the script and narration stages can execute without asking questions again. This pipeline is deliberately narrow: it produces a **narrated** video, so the brief must fix the narration language, the voice, and the duration before anything is generated.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/brief.schema.json` | Artifact validation |
| Prior artifacts | none | — |
| Tooling | none (no tools at this stage) | — |

## Process

### 1. Read the request as a production spec

Extract, in this order: topic, audience, language, duration, platform. If the request omits the language, ask once — everything downstream (voice attributes, script, captions) depends on it. Hindi is a first-class path in this pipeline: OmniVoice speaks it natively and Fableframe renders Devanagari captions.

### 2. Size the duration against the narration

Budget roughly **2.5 spoken words per second** in Hindi and English. A 60-second brief is about 150 words of narration. If the user asks for a length that the topic cannot fill, say so and propose a shorter cut rather than padding.

### 3. Write the brief

Fill the schema fields that matter:

- `title` — the working title.
- `hook` — the first sentence a viewer would hear. Write it as spoken words.
- `key_points` — 3–6 beats, each one sentence, each worth its own shot.
- `core_message` — what the viewer should be able to repeat afterwards.
- `tone`, `style` — plain words. `style` is a style playbook name when one fits, otherwise a short description of the look.
- `target_platform`, `target_duration_seconds`.
- `target_audience` — one line.
- `angle_options` — only when the topic genuinely forks; 2–3 genuinely different angles, not the same idea reworded.

Record the narration decisions in `metadata`:

```json
{
  "metadata": {
    "narration_language": "Hindi",
    "voice_attributes": "Male, Middle-aged, Indian Accent",
    "voice_mode": "design",
    "caption_script": "native"
  }
}
```

`voice_mode` is `design` (attributes) or `clone` (a reference clip; then `metadata.reference_audio` must point at a 3–10s WAV and `metadata.reference_text` at its transcript).

### 4. Name the composition runtime

This pipeline composes through Fableframe's Remotion engine. When the brief leaves room for a different treatment, **present both** runtimes and let the user choose:

- **Remotion (inside Fableframe)** — best for narrated pieces whose scenes are painted from the narration: cinematic plates, camera moves, native-script captions.
- **HyperFrames** — best for HTML/CSS/GSAP motion: kinetic typography, product promos, website-to-video, SVG character rigs. On this machine it is reached through the `hyperframes_compose` tool (`npx hyperframes`).

Say the tradeoff honestly (Remotion suits painted narrative; HyperFrames suits typographic motion) and record the choice as a `render_runtime_selection` decision in `decision_log` with **both** options listed. Do not pick a default silently — and note that HyperFrames is the runtime for OpenMontage's stock scene types, which this pipeline does not use.

### 5. Check the pipeline can do it

This pipeline renders **narrated** pieces. If the user wants a clip with no narration, or footage-led editing, say so now and hand them to `cinematic`, `hybrid`, or `documentary-montage` instead of forcing it through here.

## Quality Bar

- The hook must survive being read aloud in one breath.
- Every key point must be visually expressible — a claim that needs a screenshot or a chart is out of scope for this pipeline, because Fableframe paints scenes from narration, not from data.
- Duration must be a number, not "about a minute".

## Output

Write `brief` to `projects/<project-id>/artifacts/brief.json` and validate it against the schema before checkpointing.

## Anti-Patterns

- Vague briefs ("a video about science") that force the script stage to invent the scope.
- Promising 10 minutes of narration on a topic with three minutes of substance.
- Choosing a voice the user never asked about — voice is a creative decision, so propose it and let the gate decide.
