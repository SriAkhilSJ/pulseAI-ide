"""
tools_image.py
---------------
Image generation via Pollinations.ai (free, no signup, plain URL-based API).

Two real issues found by testing directly against the live API before
writing this, both handled explicitly below rather than assumed away:

1. Content-type mismatch: requesting an image and naming the output file
   `.png` does NOT guarantee a PNG comes back -- a real successful request
   returned `content-type: image/jpeg` (confirmed via response headers) even
   though the request had no format hint suggesting JPEG. If we blindly save
   whatever bytes come back under the caller's requested filename, a
   `banner.png` could silently contain JPEG data. This code checks the
   actual `Content-Type` response header and corrects the output filename's
   extension to match reality, returning the real path used.

2. Rate limiting: a second request fired 15s after the first one returned
   HTTP 429 with a JSON error body (`content-type: application/json`,
   `x-error-type: Too Many Requests`) instead of image bytes. Naively
   writing response.content to disk in that case would produce a file that
   *looks* like an image (has a .png name) but is actually a JSON error
   blob -- a "successful-looking" failure that would fool anything checking
   just "does the file exist / is it non-empty". This code checks the
   Content-Type BEFORE writing anything to disk and returns a clear error
   instead in that case.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import tools as _tools  # reuse _resolve / is_sensitive_path

REQUEST_TIMEOUT_S = 90  # generation can genuinely take 30-60s+ on the free tier
MAX_RETRIES_ON_RATE_LIMIT = 2
RETRY_BACKOFF_S = 8

_CONTENT_TYPE_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def _fix_extension(path: Path, content_type: str) -> Path:
    """If the real Content-Type doesn't match the requested file's
    extension, rename the target to one that matches reality (e.g. caller
    asked for banner.png but the API returned image/jpeg -> banner.jpg),
    so nothing downstream is misled by the file's name."""
    correct_ext = _CONTENT_TYPE_TO_EXT.get(content_type.split(";")[0].strip().lower())
    if correct_ext and path.suffix.lower() != correct_ext:
        return path.with_suffix(correct_ext)
    return path


def generate_image(
    prompt: str,
    output_path: str = "generated_image.png",
    width: int = 1024,
    height: int = 1024,
) -> str:
    """
    Generate an image from a text prompt using Pollinations.ai (free, no API
    key) and save it inside the project. Returns the ACTUAL path used (which
    may differ in extension from `output_path` if the API's real image
    format doesn't match what was requested -- see module docstring).
    """
    try:
        if _tools.is_sensitive_path(output_path):
            return f"ERROR: refusing to write image to sensitive path '{output_path}'."
        target = _tools._resolve(output_path)
    except Exception as e:
        return f"ERROR: invalid output_path: {e}"

    width = max(64, min(int(width), 2048))
    height = max(64, min(int(height), 2048))

    try:
        import requests
    except ImportError:
        return "ERROR: the 'requests' package is required for generate_image but is not installed."

    encoded_prompt = urllib.parse.quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?width={width}&height={height}&nologo=true&seed=42"
    )

    last_error = None
    for attempt in range(MAX_RETRIES_ON_RATE_LIMIT + 1):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT_S)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            break

        content_type = resp.headers.get("content-type", "")

        if resp.status_code == 429:
            last_error = "rate-limited (HTTP 429) by Pollinations.ai's free tier"
            if attempt < MAX_RETRIES_ON_RATE_LIMIT:
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                continue
            break

        if resp.status_code != 200:
            return (
                f"ERROR: image generation failed (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )

        if not content_type.startswith("image/"):
            # Confirmed live: a failure can come back as HTTP 200-ish
            # framing with a JSON error body instead of image bytes in some
            # cases -- never trust status code alone, check content-type
            # before writing anything to disk.
            return (
                f"ERROR: expected an image but got content-type '{content_type}'. "
                f"Response preview: {resp.text[:300]}"
            )

        real_target = _fix_extension(target, content_type)
        real_target.parent.mkdir(parents=True, exist_ok=True)
        real_target.write_bytes(resp.content)

        size_kb = real_target.stat().st_size / 1024
        note = ""
        if real_target != target:
            note = (
                f" (NOTE: saved as '{real_target.name}' not '{target.name}' -- "
                f"the API returned {content_type}, not the format implied by "
                f"the requested filename's extension.)"
            )
        return (
            f"OK: generated image saved to {real_target.relative_to(_tools.WORKDIR)} "
            f"({size_kb:.1f} KB, {content_type}).{note}\nPrompt used: {prompt!r}"
        )

    return f"ERROR: image generation failed after retries: {last_error}"


TOOL_FUNCTIONS = {"generate_image": generate_image}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image from a text description (e.g. banners, icons, "
                "hero images, illustrations for a website). Saves the image inside "
                "the project and returns the actual saved path -- check the tool's "
                "result for the real filename, since the actual image format "
                "returned by the service may not match the extension you requested."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Where to save the image, relative to the project root. Defaults to 'generated_image.png'.",
                    },
                    "width": {"type": "integer", "description": "Image width in pixels (64-2048). Defaults to 1024."},
                    "height": {"type": "integer", "description": "Image height in pixels (64-2048). Defaults to 1024."},
                },
                "required": ["prompt"],
            },
        },
    }
]
