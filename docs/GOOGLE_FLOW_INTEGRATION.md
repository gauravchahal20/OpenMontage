# Google Flow integration

> **Short version:** Google Flow has no public API. Flow is a web app for AI
> filmmaking, and the engine underneath it is **Veo 3.1** — which *does* have an
> API. Everything Flow does is reachable through the Gemini API / Vertex AI:
> generation, frames-to-video, and the consistency mechanism Flow calls
> "Scenes".

This document explains what Flow does, what maps to what, how to configure it,
and what it costs.

---

## 1. What Google Flow is, and what it is not

| | |
|---|---|
| **Google Flow** | A web application (labs.google/flow) for AI filmmaking: prompt-to-video, frames-to-video, ingredients/assets, and **Scenes** — named groups of reference images so a character or object stays the same across shots. |
| **Public API?** | **No.** Flow exposes no REST/SDK surface for automation. |
| **What powers it** | **Veo 3.1** (`veo-3.1-generate-001`, `veo-3.1-fast-generate-001`, `veo-3.1-generate-preview`). |
| **What has an API** | Veo 3.1, through the Gemini API (`GOOGLE_API_KEY`) and Vertex AI (`GOOGLE_APPLICATION_CREDENTIALS`). |

So "integrating with Flow" in a tool means: **call Veo 3.1 the way Flow calls it** —
same models, same reference-image semantics, same frame controls — and reproduce
Flow's Scenes behaviour with the API's reference-image and first/last-frame
endpoints.

There is one honest caveat: if you want Flow's *editor UI* (scenes panel, asset
library, timeline), you must use the web app. Nothing here replaces that UI; this
replaces the *engine* behind it with something a script can drive.

---

## 2. Feature mapping

| Flow feature | API equivalent | OpenMontage tool |
|---|---|---|
| Text → video | `veo-3.1-generate-001` | `veo_video`, operation `text_to_video` |
| Frames → video | image input to the same model | `veo_video`, operation `image_to_video` |
| Ingredients / assets | `VideoGenerationReferenceImage` with `reference_type=ASSET` | `veo_video`, operation `reference_to_video` |
| **Scenes** (character stays the same) | the same reference images, passed on **every** shot that features the subject | `longform_video`, `continuity: "reference"` |
| Start/end frame control | `config.last_frame` + image | `veo_video`, operation `first_last_frame_to_video` |
| Chained shots (scene continues) | previous shot's last frame as the next shot's first frame | `longform_video`, `continuity: "chain"` |

The continuity modes are implemented in `tools/video/longform_video.py`, so a
long video keeps one look from the first shot to the last.

---

## 3. Configuration

Pick **one** of these and put it in the repository `.env`:

```bash
# Option A — Gemini API (an API key from https://aistudio.google.com/apikey)
GOOGLE_API_KEY=your_key_here

# Option B — Vertex AI (a service-account JSON file)
GOOGLE_APPLICATION_CREDENTIALS=C:/path/to/service-account.json
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
```

Verify:

```bash
python -c "from tools.google_credentials import has_google_credentials; print(has_google_credentials())"
```

`False` means the Veo path cannot run. `longform_video` then reports
`degraded` and the `still` provider (local ComfyUI) remains available.

Note on the two backends: `veo_video` can also route through **fal.ai**
(`FAL_KEY`), which bills the same models but runs them on fal's infrastructure.
`backend: "auto"` prefers Google when credentials exist, fal otherwise.

---

## 4. Using it

```python
from tools.tool_registry import registry
registry.discover()

# 1. See the plan and the cost first — no billed calls.
plan = registry.get("longform_video").execute({
    "audio_path": "projects/<id>/assets/audio/narration.wav",
    "output_path": "projects/<id>/renders/final.mp4",
    "provider": "veo",
    "continuity": "reference",
    "reference_image_paths": ["assets/scene/aryan-sheet.png"],
    "dry_run": True,
})
print(plan.data["shot_count"], plan.data["estimated_cost_usd"])

# 2. Generate. One clip per shot, fitted to its narration span, joined,
#    and the original narration muxed back on top.
result = registry.get("longform_video").execute({
    "audio_path": "projects/<id>/assets/audio/narration.wav",
    "output_path": "projects/<id>/renders/final.mp4",
    "provider": "veo",
    "continuity": "reference",
    "reference_image_paths": ["assets/scene/aryan-sheet.png"],
    "style": "warm documentary photography, golden hour, 35mm, shallow depth of field",
    "shot_seconds": 7.0,
})
print(result.data["cost_usd"], result.data["failed_shots"])
```

### Continuity modes

| Mode | What it does | When to use |
|---|---|---|
| `chain` | Each shot starts from the **last frame of the previous shot**. The camera keeps moving through one continuous scene. | One continuous scene: a walk through a village, a descent, a single room. |
| `reference` | A fixed reference image (a character sheet) is passed to **every** shot. The subject stays the same while the setting changes. | A recurring character across different places — Flow's "Scenes" model. |
| `none` | Every shot is generated independently. | Montage, or when the shots are meant to be unrelated. |

With the local `still` provider the same modes work: `chain` uses image-to-image
from the previous frame, `reference` seeds every image from the reference.

---

## 5. Cost

Veo bills **generated clip seconds**, not finished runtime.

| Narration | Shots (7s each) | Generated seconds | Cost |
|---|---|---|---|
| 30s | ~5 | 40s | ~$16.00 |
| 60s | ~9 | 72s | ~$28.80 |
| 5 min | ~43 | 344s | ~$137.60 |

Rules of thumb:

- Every shot is generated at **8s** (Veo 3.1 requires 8s at 1080p), then trimmed or
  slowed to fit the narration span. You pay for all 8 seconds of every shot.
- `veo-3.1-fast-generate-001` is cheaper and faster per clip, with slightly
  weaker adherence. Use it for drafts, `veo-3.1-generate-001` for finals.
- `dry_run: true` returns the shot list and the estimate **before** any billed
  call. Always use it for the first run of a new piece.

---

## 6. What is still missing

- **Flow's UI.** The scene/asset/timeline editor has no API. This integrates the
  engine, not the editor.
- **Character sheets.** Nothing generates a consistent reference image for you.
  Produce one (with `comfyui_image` or a photo) and pass it in
  `reference_image_paths`.
- **Lip-synced dialogue.** Veo generates its own audio for a clip, but this
  pipeline discards it and muxes the narration track instead — the narration is
  the source of truth.
