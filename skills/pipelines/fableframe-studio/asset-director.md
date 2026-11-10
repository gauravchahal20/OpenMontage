# Asset Director — Fableframe Studio Pipeline

## When To Use

You are turning the approved script into audio on disk. In this pipeline the only asset you generate at this stage is the **narration** — Fableframe generates the visuals itself in the compose stage, because it derives them from the audio.

## Prerequisites

| Layer | Resource | Purpose |
|---|---|---|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["script"]["script"]`, `state.artifacts["idea"]["brief"]` | Lines to speak, voice to speak them with |
| Tool | `omnivoice_tts` | Local zero-shot TTS on the GPU |
| Layer 3 skill | `.agents/skills/text-to-speech/SKILL.md` | Read before writing voice prompts |

## Process

### 1. Announce the provider before generating

State the tool name, the provider, the model, and the cost before the first call. For this pipeline that is:

```
Tool: omnivoice_tts | Provider: OmniVoice (local) | Model: k2-fsa/OmniVoice
Runtime: LOCAL_GPU | Cost: $0.00 | Speed: RTF ~0.4 (faster than real time)
```

No API key is involved and nothing leaves the machine. If OmniVoice is unavailable, `piper_tts` is the fallback — say so, and note that piper has no Hindi voice beyond `hi_IN-pratham-medium` and no voice design.

### 2. Map script sections to lines

One line per script section. Keep the section `id` as the segment id so the manifest can be traced back to the script.

For a multi-voice script, give each line its own `instruct` from `metadata.speakers`. Voice attributes are a **whitelist** in OmniVoice — gender, age, pitch, dialect/accent — not free-form prose. "Male, Middle-aged, Indian Accent" works; "a warm grandfatherly village uncle" does not.

### 3. Generate

Call the tool once with every line, not once per line. The model loads in ~20s and is reused across the batch; per-line calls would pay that cost repeatedly.

```python
from tools.tool_registry import registry
registry.discover()
result = registry.get("omnivoice_tts").execute({
    "lines": [
        {"id": "sec01", "text": "...", "instruct": "Male, Middle-aged, Indian Accent"},
        {"id": "sec02", "text": "...", "instruct": "Male, Middle-aged, Indian Accent"},
    ],
    "output_path": "<project>/assets/audio/narration.wav",
    "segments_dir": "<project>/assets/audio/segments",
    "num_step": 16,
    "gap_seconds": 0.34,
})
```

- `num_step` 16 is the working default; 32 for a final take, 8 for a throwaway test.
- `gap_seconds` 0.34 reads as a natural breath between lines. Raise it for a slower, more formal delivery.

The tool returns the combined narration path plus the per-segment paths and the real durations.

### 4. Verify before checkpointing

Listen to the combined narration — do not just trust the file size. Check that:

- the language is the language in the brief,
- the voice matches the approved attributes,
- no line is truncated or has a false start,
- the total duration is within 10% of the brief's target.

If a line is wrong, regenerate that line only and re-concatenate. Do not re-run the whole script for one bad take.

### 5. Write the manifest

One asset entry per narration file, plus the combined track:

```json
{
  "version": "1.0",
  "assets": [
    {"id": "narration-combined", "type": "narration",
     "path": "assets/audio/narration.wav", "source_tool": "omnivoice_tts",
     "scene_id": "all", "duration_seconds": 41.2, "provider": "omnivoice",
     "model": "k2-fsa/OmniVoice"},
    {"id": "sec01", "type": "narration",
     "path": "assets/audio/segments/sec01.wav", "source_tool": "omnivoice_tts",
     "scene_id": "sec01", "duration_seconds": 9.8, "provider": "omnivoice"}
  ],
  "total_cost_usd": 0.0,
  "metadata": {"narration_language": "Hindi", "voice_attributes": "..."}
}
```

Paths are relative to the project directory.

## Music

Background music is optional here and comes from `music_gen` (API, costs money) or the user's `music_library/`. Fableframe's render carries the narration on its own audio track; adding music means mixing it in the compose stage. Say which you are doing — do not add music silently.

## Quality Bar

- Every script section has audio.
- The narration is in the right language and voice.
- Total duration within 10% of the brief target.

## Anti-Patterns

- Generating a line per tool call (pays the model load over and over).
- Free-form voice descriptions that the attribute whitelist rejects.
- Checkpointing without listening to the output.
