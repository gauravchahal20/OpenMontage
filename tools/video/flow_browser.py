"""Drive the Google Flow web surface to generate clips.

Google Flow has **no API key** — videos are made in the web app at
flow.google.com, which needs a signed-in Google account (Flow ships with
Google One AI Premium). So the way to automate it is to drive the real page.

This tool opens the installed Chrome through Playwright with a **persistent
profile**, so the Google sign-in is kept between runs:

    operation="login"    one time: opens Chrome visibly, waits for you to sign in
    operation="check"    reports whether the session is still signed in
    operation="generate" types a prompt, optionally attaches reference images
                         (Flow's ingredients / Scenes), generates, downloads the clip

Honest limits, because a web surface is not an API:

- Google ships UI changes without notice. Every selector is resolved through
  several strategies and can be re-pointed in ``flow_selectors.json`` without
  touching this file.
- If a step cannot be found, the tool saves a screenshot and the page HTML
  next to the output and **fails**. It never invents a clip.
- Automating a Google web app may conflict with Google's terms. Use an account
  and a quota you are allowed to automate.

This is the ``flow`` clip provider for ``longform_video``: Flow makes the
clips, the assembler fits them to the narration and muxes the audio.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTORS_PATH = Path(__file__).resolve().parent / "flow_selectors.json"
PROFILE_DIR = Path(
    os.environ.get("FLOW_PROFILE_DIR", Path.home() / ".openmontage" / "flow_profile")
)

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

# Used when tools/video/flow_selectors.json is missing or broken.
DEFAULT_SELECTORS: dict[str, Any] = {
    "app_url": "https://flow.google.com/",
    "prompt": {"css": ["textarea[placeholder*='prompt' i]", "textarea"], "role_textbox_names": ["prompt"]},
    "generate_button": {
        "css": ["button[aria-label*='Generate' i]"],
        "role_button_names": ["generate", "generate video"],
        "text_regex": "^generate$|generate video",
    },
    "reference_upload": {
        "css": ["input[type='file']", "[aria-label*='add image' i]", "[aria-label*='ingredient' i]"],
        "role_names": ["add image", "upload", "ingredients"],
    },
    "download_button": {
        "css": ["a[download]", "button[aria-label*='download' i]"],
        "role_button_names": ["download", "download video"],
        "text_regex": "download",
    },
    "video_ready": {"css": ["video"], "wait_for_video_element": True},
    "signed_out_markers": ["sign in", "log in", "continue with google"],
    "signed_in_markers": ["new scene", "create scene", "ingredients"],
    "enter_app": {
        "css": [],
        "role_button_names": ["Create with Google Flow", "Try in Google Flow", "Open Flow"],
        "role_link_names": ["Create with Google Flow", "Try in Google Flow", "Open Flow"],
        "text_regex": "create with google flow|try in google flow|open flow",
    },
}


def _load_selectors() -> dict:
    """Load the selector map.

    A malformed file must not silently produce an empty map — that would make
    every lookup fail with a confusing "could not find the prompt box". Fall
    back to the built-in defaults and say so.
    """
    try:
        data = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[flow_browser] {SELECTORS_PATH} could not be read ({exc}); using defaults.")
        return dict(DEFAULT_SELECTORS)
    if not isinstance(data, dict) or not data:
        print(f"[flow_browser] {SELECTORS_PATH} is empty; using defaults.")
        return dict(DEFAULT_SELECTORS)
    return data


def _chrome_executable() -> str | None:
    override = os.environ.get("FLOW_CHROME_PATH")
    if override and Path(override).is_file():
        return override
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return None


class FlowBrowser(BaseTool):
    name = "flow_browser"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "flow"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    dependencies = []
    install_instructions = (
        "Install Playwright in this repository's venv and use the Chrome that is\n"
        "already on the machine (no separate browser download is needed):\n"
        "  .venv\\Scripts\\pip install playwright\n"
        "Then sign in once:\n"
        "  .venv\\Scripts\\python.exe -c \"from tools.tool_registry import registry; "
        "registry.discover(); print(registry.get('flow_browser').execute("
        "{'operation': 'login'}).data)\"\n"
        "Google Flow needs a Google account with Google One AI Premium."
    )

    agent_skills = ["ai-video-gen", "video-toolkit"]

    capabilities = ["text_to_video", "reference_to_video", "web_surface_automation"]
    supports = {
        "text_to_video": True,
        "image_to_video": True,
        "reference_to_video": True,
        "first_last_frame_to_video": False,
        "native_audio": True,
    }
    best_for = [
        "generating clips with the exact Google Flow interface (Scenes, ingredients, camera controls)",
        "using a Flow subscription instead of per-second API billing",
    ]
    not_good_for = [
        "unattended batch work (the UI must be driven interactively)",
    ]
    fallback_tools = ["veo_video", "longform_video"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["login", "check", "generate"],
                "default": "generate",
            },
            "prompt": {
                "type": "string",
                "description": "The scene description to type into Flow.",
            },
            "reference_image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Images to attach as Flow ingredients / Scene references, so the "
                    "same subject appears across shots."
                ),
            },
            "output_path": {"type": "string", "description": "Where to save the downloaded clip."},
            "aspect": {"type": "string", "enum": ["16:9", "9:16"], "default": "16:9"},
            "resolution": {"type": "string", "enum": ["720p", "1080p"], "default": "1080p"},
            "duration_seconds": {"type": "integer", "default": 8, "minimum": 4, "maximum": 8},
            "headless": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Headless is risky: Google may block or challenge an automated "
                    "session. Keep it False."
                ),
            },
            "timeout_seconds": {"type": "integer", "default": 900},
            "keep_open": {
                "type": "boolean",
                "default": False,
                "description": "Leave the browser open after the operation (debugging).",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=2048, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["prompt", "operation", "aspect", "resolution"]
    side_effects = [
        "opens a browser window signed into your Google account",
        "writes the downloaded clip to output_path",
    ]
    user_visible_verification = [
        "Watch the clip and confirm the prompt was interpreted as intended",
    ]

    def get_status(self) -> ToolStatus:
        if _chrome_executable() is None:
            return ToolStatus.UNAVAILABLE
        try:
            import playwright  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Flow runs on a subscription, not per-second billing.
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 240.0

    # ------------------------------------------------------------------
    # Browser plumbing
    # ------------------------------------------------------------------

    def _launch(self, headless: bool):
        from playwright.sync_api import sync_playwright

        chrome = _chrome_executable()
        if chrome is None:
            raise RuntimeError("Chrome or Edge not found on this machine.")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            executable_path=chrome,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            viewport={"width": 1600, "height": 1000},
        )
        return playwright, context

    @staticmethod
    def _first_match(page, spec: dict) -> Any:
        """Try CSS, then role/name, then text — the first *visible* hit wins."""
        for selector in spec.get("css", []) or []:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        for name in spec.get("role_button_names", []) or []:
            try:
                locator = page.get_by_role("button", name=name)
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        pattern = spec.get("text_regex")
        if pattern:
            try:
                locator = page.get_by_role("button", name=re.compile(pattern, re.I))
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                pass
        for name in spec.get("role_textbox_names", []) or []:
            try:
                locator = page.get_by_role("textbox", name=name)
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _save_failure(page, output_path: Path, tag: str) -> list[str]:
        """Screenshot + HTML so a failure can be diagnosed instead of guessed."""
        saved: list[str] = []
        try:
            base = output_path.parent if output_path else Path.cwd()
            base.mkdir(parents=True, exist_ok=True)
            shot = base / f"flow-failure-{tag}.png"
            page.screenshot(path=str(shot), full_page=True)
            saved.append(str(shot))
            html = base / f"flow-failure-{tag}.html"
            html.write_text(page.content(), encoding="utf-8")
            saved.append(str(html))
        except Exception:
            pass
        return saved

    @staticmethod
    def _click_visible(locator, timeout: int = 20000) -> bool:
        """Click the first *visible* match.

        Google's pages render duplicated markup (one copy hidden), so
        ``.first`` can land on an invisible element and the click silently
        times out. Iterate and take the first one that is actually visible.
        """
        try:
            total = min(locator.count(), 6)
        except Exception:
            return False
        for index in range(total):
            item = locator.nth(index)
            try:
                if not item.is_visible():
                    continue
                item.click(timeout=timeout)
                return True
            except Exception:
                continue
        return False

    def _enter_app(self, page, selectors: dict) -> bool:
        """Click through the marketing page into the Flow app.

        ``flow.google.com/`` redirects to the public "about" page, which has a
        "Create with Google Flow" button that leads to the app (and to the
        sign-in wall when the profile has no session).
        """
        prompt_css = ", ".join((selectors.get("prompt", {}) or {}).get("css", [])[:1]) or "textarea"
        try:
            if page.locator(prompt_css).count():
                return True
        except Exception:
            pass

        spec = selectors.get("enter_app", {}) or {}
        for name in spec.get("role_button_names", []) or []:
            try:
                if self._click_visible(page.get_by_role("button", name=name)):
                    page.wait_for_timeout(5000)
                    return True
            except Exception:
                continue
        for name in spec.get("role_link_names", []) or []:
            try:
                if self._click_visible(page.get_by_role("link", name=name)):
                    page.wait_for_timeout(5000)
                    return True
            except Exception:
                continue
        pattern = spec.get("text_regex")
        if pattern:
            for role in ("button", "link"):
                try:
                    if self._click_visible(page.get_by_role(role, name=re.compile(pattern, re.I))):
                        page.wait_for_timeout(5000)
                        return True
                except Exception:
                    continue
            try:
                if self._click_visible(page.get_by_text(re.compile(pattern, re.I))):
                    page.wait_for_timeout(5000)
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _session_state(page, selectors: dict) -> str:
        """One of ``signed_in`` / ``signed_out`` / ``unknown``.

        The root URL redirects to a marketing page, which has no sign-in wording
        but also is not the app. Absence of "sign in" therefore does not mean
        signed in: the app itself has to be on screen.
        """
        try:
            body = (page.inner_text("body", timeout=5000) or "").lower()
        except Exception:
            body = ""

        for marker in selectors.get("signed_out_markers") or []:
            if marker.lower() in body:
                return "signed_out"

        try:
            if page.locator(
                ", ".join((selectors.get("prompt", {}) or {}).get("css", [])[:1]) or "textarea"
            ).count():
                return "signed_in"
        except Exception:
            pass
        try:
            if page.get_by_role("button", name=re.compile(r"^generate$", re.I)).count():
                return "signed_in"
        except Exception:
            pass
        for marker in selectors.get("signed_in_markers") or []:
            if marker.lower() in body:
                return "signed_in"
        return "unknown"

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error="Playwright or Chrome is missing. " + self.install_instructions,
            )

        operation = str(inputs.get("operation") or "generate").lower()
        selectors = _load_selectors()
        output_path = Path(inputs.get("output_path") or "flow_clip.mp4")
        headless = bool(inputs.get("headless", False))
        timeout = int(inputs.get("timeout_seconds") or 900)

        playwright = None
        context = None
        started = time.time()
        try:
            playwright, context = self._launch(headless)
            page = context.pages[0] if context.pages else context.new_page()
            # Playwright timeouts are milliseconds; the tool's are seconds.
            page.set_default_timeout(timeout * 1000)

            if operation == "login":
                page.goto(selectors.get("app_url", "https://flow.google.com/"), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                self._enter_app(page, selectors)
                state = self._session_state(page, selectors)
                if state != "signed_in":
                    print(
                        "A Chrome window is open at Google Flow.\n"
                        "Sign in there (it stays signed in for later runs). The tool returns\n"
                        "as soon as the Flow app is detected."
                    )
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        page.wait_for_timeout(3000)
                        state = self._session_state(page, selectors)
                        if state == "signed_in":
                            break
                signed_in = state == "signed_in"
                return ToolResult(
                    success=signed_in,
                    data={
                        "signed_in": signed_in,
                        "state": state,
                        "profile_dir": str(PROFILE_DIR),
                        "note": "The sign-in is stored in the profile and reused by later runs.",
                    },
                    error=None if signed_in else (
                        "The Flow app did not appear. Sign in at flow.google.com in the "
                        "window that opened, then run operation='login' again."
                    ),
                    duration_seconds=round(time.time() - started, 2),
                )

            if operation == "check":
                page.goto(selectors.get("app_url", "https://flow.google.com/"), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                self._enter_app(page, selectors)
                state = self._session_state(page, selectors)
                return ToolResult(
                    success=state == "signed_in",
                    data={"signed_in": state == "signed_in", "state": state,
                          "profile_dir": str(PROFILE_DIR)},
                    error=None if state == "signed_in" else (
                        "The Flow app is not available in this browser profile. "
                        "Run operation='login' and sign in at flow.google.com."
                    ),
                    duration_seconds=round(time.time() - started, 2),
                )

            # --- generate -------------------------------------------------
            prompt = (inputs.get("prompt") or "").strip()
            if not prompt:
                return ToolResult(success=False, error="prompt is required for operation='generate'.")

            page.goto(selectors.get("app_url", "https://flow.google.com/"), wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._enter_app(page, selectors)

            state = self._session_state(page, selectors)
            if state != "signed_in":
                saved = self._save_failure(page, output_path, "signed-out")
                return ToolResult(
                    success=False,
                    error=(
                        f"The Flow app is not available in this browser profile (state: {state}). "
                        "Run operation='login' once and sign in at flow.google.com, then retry. "
                        + (f"Screenshot: {saved[0]}" if saved else "")
                    ),
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )

            # Reference images — Flow's ingredients / Scene references.
            references = [str(p) for p in (inputs.get("reference_image_paths") or []) if Path(p).is_file()]
            if references:
                uploader = self._first_match(page, selectors.get("reference_upload", {}))
                if uploader is not None:
                    try:
                        uploader.set_input_files(references)
                    except Exception:
                        # The control may be a button that opens a file chooser.
                        try:
                            with page.expect_file_chooser(timeout=15000) as chooser_info:
                                uploader.click()
                            chooser_info.value.set_files(references)
                        except Exception as exc:
                            saved = self._save_failure(page, output_path, "upload")
                            return ToolResult(
                                success=False,
                                error=f"Could not attach the reference images: {exc}",
                                artifacts=saved,
                                duration_seconds=round(time.time() - started, 2),
                            )

            prompt_box = self._first_match(page, selectors.get("prompt", {}))
            if prompt_box is None:
                saved = self._save_failure(page, output_path, "no-prompt-box")
                return ToolResult(
                    success=False,
                    error=(
                        "Could not find the prompt box on the Flow page. The UI may have "
                        "changed — update tools/video/flow_selectors.json. "
                        + (f"Screenshot: {saved[0]}" if saved else "")
                    ),
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )
            prompt_box.click()
            prompt_box.fill(prompt)

            generate = self._first_match(page, selectors.get("generate_button", {}))
            if generate is None:
                saved = self._save_failure(page, output_path, "no-generate")
                return ToolResult(
                    success=False,
                    error=(
                        "Could not find the Generate button. The UI may have changed — "
                        "update tools/video/flow_selectors.json. "
                        + (f"Screenshot: {saved[0]}" if saved else "")
                    ),
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )
            generate.click()

            # Wait for the clip to render, then download it.
            video_spec = selectors.get("video_ready", {})
            try:
                page.wait_for_selector(
                    ", ".join(video_spec.get("css", ["video"])), timeout=timeout, state="attached"
                )
            except Exception:
                pass
            page.wait_for_timeout(4000)

            download = self._first_match(page, selectors.get("download_button", {}))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if download is None:
                saved = self._save_failure(page, output_path, "no-download")
                return ToolResult(
                    success=False,
                    error=(
                        "The clip was generated but no download control was found. The UI may "
                        "have changed — update tools/video/flow_selectors.json. "
                        + (f"Screenshot: {saved[0]}" if saved else "")
                    ),
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )

            try:
                with page.expect_download(timeout=120000) as download_info:
                    download.click()
                saved_file = download_info.value
                saved_file.save_as(str(output_path))
            except Exception as exc:
                saved = self._save_failure(page, output_path, "download-failed")
                return ToolResult(
                    success=False,
                    error=f"The download did not complete: {exc}",
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )

            if not output_path.is_file():
                saved = self._save_failure(page, output_path, "missing-file")
                return ToolResult(
                    success=False,
                    error="Flow reported a download but no file was written.",
                    artifacts=saved,
                    duration_seconds=round(time.time() - started, 2),
                )

            return ToolResult(
                success=True,
                data={
                    "video_path": str(output_path),
                    "prompt": prompt,
                    "reference_images": references,
                    "profile_dir": str(PROFILE_DIR),
                    "size_bytes": output_path.stat().st_size,
                    "source": "google-flow-web",
                },
                artifacts=[str(output_path)],
                cost_usd=0.0,
                duration_seconds=round(time.time() - started, 2),
            )

        except Exception as exc:  # noqa: BLE001 - report the real failure
            saved: list[str] = []
            if context is not None:
                try:
                    page = context.pages[0] if context.pages else None
                    if page is not None:
                        saved = self._save_failure(page, output_path, "exception")
                except Exception:
                    pass
            return ToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                artifacts=saved,
                duration_seconds=round(time.time() - started, 2),
            )
        finally:
            if context is not None and not inputs.get("keep_open"):
                try:
                    context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
