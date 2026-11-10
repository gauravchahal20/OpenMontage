# Script Director — Fableframe Studio Pipeline

## When To Use

You are writing the words that will be spoken. In this pipeline the script is not a pre-text for editing — it **is** the video. Fableframe transcribes this audio and paints a scene per sentence, so every sentence you write becomes a shot.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/script.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["idea"]["brief"]` | Topic, language, voice, duration |
| Voice | `metadata.narration_language`, `metadata.voice_attributes` from the brief | How the lines must sound |

## Process

### 1. Write for the ear, not the page

- Short sentences. One idea per sentence — a sentence that needs a comma and a conjunction becomes two shots.
- No parentheticals, no bullet lists, no markdown. Spoken text only.
- Numbers, names and technical terms are read literally by the TTS. Write what should be *heard* ("twenty four", "Doctor Sharma"), not what looks right on the page.

### 2. Structure for the cut

Open with the brief's hook verbatim. Then one section per key point, in the brief's order. Close with the core message as a final line. The first section and the last section are what a viewer keeps.

### 3. Size it to the duration

Target `brief.target_duration_seconds` at roughly 2.5 words per second. Fableframe will lay scenes across the real audio, so being close matters more than being exact — but land within 10% of the brief.

### 4. Assign sections and timings

Each section needs `id`, `text`, `start_seconds`, `end_seconds`. Timings are the **expected** timings used for planning; Fableframe re-derives the true word timings from the synthesised audio, so treat these as the intent, not a contract.

For a single narrator, one section per key point is usually right. For a dialogue, one section per line and set `metadata.voice_attributes` per line in the asset stage.

### 5. Multi-voice pieces

Record the voice for each line in `metadata.speakers`:

```json
{"metadata": {"speakers": {
  "narrator": "Male, Middle-aged, Indian Accent",
  "child":   "Male, Child, Indian Accent, High pitch"
}}}
```

Keep speaker turns short (one or two sentences). Long turns make the voice jump, and the asset stage concatenates lines with a fixed gap, so a long turn reads as a monologue.

## Quality Bar

- Every key point in the brief appears exactly once.
- The script reads naturally aloud, in the target language, without stumbling.
- No sentence exists only to satisfy the word count.

## Output

Write `script` to `projects/<project-id>/artifacts/script.json` and validate it against the schema before checkpointing.

## Anti-Patterns

- On-screen text disguised as narration ("In this video, we will explore...").
- Sentences long enough that Fableframe paints a whole paragraph into one scene.
- Technical vocabulary the target audience would not know, stated without explanation.
