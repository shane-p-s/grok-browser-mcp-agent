"""Playwright prefill for secrets before Browser Use agent (values never sent to LLM)."""

from __future__ import annotations

import logging
from typing import Any

import secrets_store
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def _validate_prefill(prefill: list[Any]) -> str | None:
    if not isinstance(prefill, list) or not prefill:
        return "secret_prefill must be a non-empty list"
    for i, step in enumerate(prefill):
        if not isinstance(step, dict):
            return f"secret_prefill[{i}] must be an object"
        url = step.get("url")
        fills = step.get("fills")
        if not url or not isinstance(url, str):
            return f"secret_prefill[{i}].url is required"
        if not url.startswith("https://"):
            return f"secret_prefill[{i}].url must start with https://"
        if not fills or not isinstance(fills, list):
            return f"secret_prefill[{i}].fills must be a non-empty list"
        for j, f in enumerate(fills):
            if not isinstance(f, dict):
                return f"secret_prefill[{i}].fills[{j}] must be an object"
            if not (f.get("selector") and isinstance(f["selector"], str)):
                return f"secret_prefill[{i}].fills[{j}].selector is required"
            if not (f.get("secret_name") and isinstance(f["secret_name"], str)):
                return f"secret_prefill[{i}].fills[{j}].secret_name is required"
            err = secrets_store.validate_secret_name(f["secret_name"])
            if err:
                return f"secret {f['secret_name']!r}: {err}"
    return None


async def run_secret_prefill(
    prefill: list[Any],
    headed: bool,
    user_data_dir: str | None,
) -> str | None:
    """
    Run Playwright fills locally. Returns error string or None on success.
    """
    err = _validate_prefill(prefill)
    if err:
        return err
    if not secrets_store.master_key_configured():
        return "SECRETS_MASTER_KEY not configured; cannot resolve secrets for prefill"

    headless = not headed
    try:
        async with async_playwright() as p:
            browser = None
            if user_data_dir:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=headless,
                )
            else:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context()
            try:
                pages = context.pages
                page = pages[0] if pages else await context.new_page()
                for step in prefill:
                    assert isinstance(step, dict)
                    url = str(step["url"])
                    fills = step["fills"]
                    assert isinstance(fills, list)
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    for f in fills:
                        assert isinstance(f, dict)
                        sel = str(f["selector"])
                        name = str(f["secret_name"])
                        val = secrets_store.get_secret(name)
                        if val is None:
                            return f"unknown_or_missing_secret:{name}"
                        await page.fill(sel, val, timeout=30000)
            finally:
                await context.close()
                if browser is not None:
                    await browser.close()
    except Exception as e:
        logger.exception("secret_prefill failed")
        return f"prefill_error:{type(e).__name__}:{str(e)[:400]}"
    return None
