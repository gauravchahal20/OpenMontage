# Google Flow integration

> **Short version:** Google Flow has no API key. Videos are made in the web app at
> **flow.google.com**, which needs a signed-in Google account (Flow ships with
> Google One AI Premium). So the integration **drives the real page** — with the
> Chrome you already have, using a persistent profile that keeps you signed in.
>
> There is a second path: Flow's engine is **Veo 3.1**, which *does* have an API
> (Gemini API / Vertex AI). If you have a key and prefer a scriptable, reliable
> pipeline, use `provider: "veo"`.

This document explains what Flow is, how the web-surface integration works, how to
configure it, and what it costs.

---

## 1. Two ways to use Google

| | `provider: "flow"` | `provider: "veo"` |
|---|---|---|
| How | Drives **flow.google.com** in your signed-in Chrome | Calls the Veo 3.1 API |
| Credentials | A Google account signed in once (Flow = Google One AI Premium) | `GOOGLE_API_KEY` or Vertex service account |
| Billing | Your Flow subscription | ~$0.40 per **generated** second |
| Reliability | The UI can change; a change means updating selectors | Stable API |
| Speed | Slower (browser round-trips, UI queue) | Faster |
| Quality | Exactly Flow's Scenes/ingredients/camera controls | The same model, fewer controls |

Pick **flow** when you want Flow's interface features and your subscription.
Pick **veo** when you want unattended, repeatable, scriptable runs.

---

## 2. How the web-surface integration works

`tools/video/flow_browser.py` opens **the Chrome already installed on the machine**
through Playwright, with a persistent profile at:

```
%USERPROFILE%\.openmontage\flow_profile
```

Because the profile persists, **you sign in once** and every later run is already
signed in. Three operations:

| Operation | What it does |
|---|---|
| `login` | Opens a visible Chrome window at Flow, waits until you are signed in, then returns. Run it once. |
| `check` | Reports whether the session is still signed in. |
| `generate` | Types the prompt, attaches reference images (Flow ingredients / Scene references), clicks Generate, waits for the clip, downloads it. |

**It never invents a clip.** If the prompt box, the Generate button or the download
control cannot be found, the tool saves a screenshot and the page HTML next to the
output and fails with the path. Google ships UI changes without notice, so every
selector lives in `tools/video/flow_selectors.json` — CSS, then role/label, then
placeholder — and can be re-pointed without touching Python.

---

## 3. Feature mapping

| Flow feature | How it is used here |
|---|---|
| Text → video | the shot prompt is typed into the Flow prompt box |
| Ingredients / assets | `reference_image_paths` are attached before generating — a character sheet keeps the same subject across shots |
| Scenes | pass the same reference images on every shot that features the subject |
| Camera controls | express them in the prompt text ("35mm, low angle, slow push-in") |
| Chained shots | the previous shot's last frame is attached as an ingredient, so the next shot continues the scene (`continuity: "chain"`) |

---

## 4. Configuration

Install Playwright into the repository venv (it drives your installed Chrome — no
separate browser download):

```bash
.venv\Scripts\pip install playwright
```

Then sign in once:

```python
from tools.tool_registry import registry
registry.discover()
print(registry.get("flow_browser").execute({"operation": "login"}).data)
```

A Chrome window opens at flow.google.com. Sign in there; the session is kept in the
profile. Verify later with `{"operation": "check"}`.

---

## 5. Using it

```python
from tools.tool_registry import registry
registry.discover()

# 1. Plan and price first — no browser, no generation.
plan = registry.get("longform_video").execute({
    "audio_path": "projects/<id>/assets/audio/narration.wav",
    "output_path": "projects/<id>/renders/final.mp4",
    "provider": "flow",
    "continuity": "reference",
    "reference_image_paths": ["assets/scene/aryan-sheet.png"],
    "dry_run": True,
})
print(plan.data["shot_count"])

# 2. Generate each shot in Flow, fit it to its narration span, join, and mux
#    the original narration back on top.
result = registry.get("longform_video").execute({
    "audio_path": "projects/<id>/assets/audio/narration.wav",
    "output_path": "projects/<id>/renders/final.mp4",
    "provider": "flow",
    "continuity": "reference",
    "reference_image_paths": ["assets/scene/aryan-sheet.png"],
    "style": "warm documentary photography, golden hour, 35mm",
    "shot_seconds": 7.0,
})
print(result.data["failed_shots"])
```

The browser opens visibly during a `flow` run — that is deliberate: an invisible
automated Google session is more likely to be challenged, and you want to see what
is being generated.

---

## 6. Cost

Flow runs on your subscription, so the tool reports **$0.00**. The real limit is
Flow's own quota (credits per month on the account), not a per-second price.

For comparison, the API path bills **generated clip seconds**:

| Narration | Shots (7s each) | Generated seconds | Cost |
|---|---|---|---|
| 30s | ~5 | 40s | ~$16.00 |
| 60s | ~9 | 72s | ~$28.80 |
| 5 min | ~43 | 344s | ~$137.60 |

Every shot is generated at 8s (Veo 3.1 requires 8s at 1080p) and then trimmed or
slowed to fit its narration span — you pay for all 8 seconds of every shot.

---

## 7. What is still missing

- **Flow's editor UI** stays a manual tool: this generates clips, it does not
  build a scene graph in Flow's own timeline.
- **Character sheets** are not generated for you. Produce one (with `comfyui_image`
  or a photo) and pass it in `reference_image_paths`.
- **Prompt drift.** The Flow UI decides how to interpret the prompt; the tool
  cannot verify the semantics, only that a clip came back.
- **Terms of service.** Automating a Google web app may conflict with Google's
  terms. Use an account and a quota you are allowed to automate.
