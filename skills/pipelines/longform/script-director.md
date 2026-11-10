# Script Director — Google Longform Pipeline

## When To Use

You are producing the timed text of the narration. In this pipeline the audio
already exists — the script is the **transcript**, and its job is to give every
word a timestamp so shots can be aligned to it.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/script.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["idea"]["brief"]` | Narration path, duration, language |
| Tool | `transcriber` | Word-level timings (faster-whisper locally, WhisperX or cloud optionally) |

## Process

### 1. Transcribe the audio

```python
from tools.tool_registry import registry
registry.discover()
result = registry.get("transcriber").execute({
    "input_path": "<project>/assets/audio/narration.wav",
    "output_dir": "<project>/artifacts",
})
```

`result.data` carries `text`, `segments` and `word_timestamps`. Keep the word
timings — they are what the shot planner aligns to.

For non-English narration, keep the original script. A translation belongs in the
captions, not in the transcript, unless the user asked for a dubbed version.

### 2. Build the sections

One section per natural beat of the narration — a sentence, or two short
sentences. Each section needs `id`, `text`, `start_seconds`, `end_seconds`.
Take the timings from the transcript; do not estimate them.

Sections are the units the shot planner cuts on. A 70-second narration usually
lands 10–14 sections.

### 3. Check coverage

The last section's `end_seconds` should reach the audio duration (within ~0.3s),
and the first should start at ~0. Gaps mean the transcript missed something —
re-run the transcriber with a different model or language setting rather than
inventing words.

## Quality Bar

- Every word of the audio appears in the transcript.
- Timings are monotonic and cover the whole track.
- Section boundaries fall on sentence ends, not mid-clause.

## Output

Write `script` to `projects/<project-id>/artifacts/script.json`, validate against
the schema, then checkpoint.

## Anti-Patterns

- Hand-typed timings — they drift and the shots desynchronise.
- One section for the entire track, which forces one 70-second shot.
- Translating the transcript when the user asked for subtitles in the same language.
