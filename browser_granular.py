"""Fast CDP browser actions for Grok (no Browser Use / DeepSeek agent on the hot path)."""

from __future__ import annotations

import logging
import os
from typing import Any

import browser_hub

logger = logging.getLogger(__name__)


def _page_state_max_elements() -> int:
    try:
        return max(10, min(int(os.getenv("BROWSER_PAGE_STATE_MAX_ELEMENTS", "80")), 200))
    except ValueError:
        return 80


def _element_summary(node: Any, index: int) -> dict[str, Any]:
    attrs = node.attributes or {}
    text = (node.node_value or "").strip()
    if not text and attrs.get("aria-label"):
        text = str(attrs.get("aria-label", "")).strip()
    if len(text) > 160:
        text = text[:160] + "…"
    return {
        "index": index,
        "tag": (node.node_name or "").lower(),
        "text": text or None,
        "role": attrs.get("role"),
        "id": attrs.get("id"),
        "name": attrs.get("name"),
        "type": attrs.get("type"),
        "placeholder": attrs.get("placeholder"),
    }


async def open_tab(
    label: str = "",
    *,
    headed: bool,
    user_data_dir: str | None,
    url: str | None = None,
    reuse_existing_tab: bool = True,
) -> dict[str, Any]:
    text_label = (label or "grok_tab").strip()[:200] or "grok_tab"
    if reuse_existing_tab and browser_hub.normalize_tab_label(text_label):
        existing = browser_hub.find_idle_tab_by_label(text_label)
        if existing:
            browser_hub.touch_idle_tab_reuse(existing.tab_id)
            out: dict[str, Any] = {
                "success": True,
                "tab_id": existing.tab_id,
                "status": "idle",
                "tab_label": existing.label,
                "tab_reused": True,
            }
            if url and url.strip():
                nav = await navigate(existing.tab_id, url.strip())
                if nav.get("error"):
                    out["navigate_error"] = nav
                else:
                    out.update({k: v for k, v in nav.items() if k != "tab_id"})
            return out

    tab_id, err = await browser_hub.open_granular_tab(
        text_label,
        headed=headed,
        user_data_dir=user_data_dir,
    )
    if err:
        return {"error": err}
    out = {"success": True, "tab_id": tab_id, "status": "idle", "tab_label": text_label, "tab_reused": False}
    if url and url.strip():
        nav = await navigate(tab_id, url.strip())
        if nav.get("error"):
            out["navigate_error"] = nav
        else:
            out.update({k: v for k, v in nav.items() if k != "tab_id"})
    return out


async def navigate(tab_id: str, url: str) -> dict[str, Any]:
    if not url.strip().startswith(("https://", "http://")):
        return {"error": "url_must_be_http_or_https", "tab_id": tab_id}
    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id}
    try:
        from browser_use.browser.events import NavigateToUrlEvent

        await session.event_bus.dispatch(NavigateToUrlEvent(url=url.strip(), new_tab=False))
        await browser_hub.sync_tab_metadata(tab_id, session)
        rec = browser_hub.get_tab_record(tab_id)
        return {
            "success": True,
            "tab_id": tab_id,
            "url": rec.url if rec else url,
            "title": rec.title if rec else None,
        }
    except Exception as e:
        logger.warning("browser_navigate %s: %s", tab_id, e)
        return {"error": "navigate_failed", "detail": type(e).__name__, "tab_id": tab_id}


async def get_page_state(tab_id: str) -> dict[str, Any]:
    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id}
    try:
        state = await session.get_browser_state_summary(
            include_screenshot=False,
            cached=False,
            include_recent_events=False,
        )
        selector_map = await session.get_selector_map()
        if not selector_map and state.dom_state is not None:
            selector_map = getattr(state.dom_state, "selector_map", None) or {}
        max_n = _page_state_max_elements()
        elements: list[dict[str, Any]] = []
        for idx in sorted(selector_map.keys())[:max_n]:
            node = selector_map[idx]
            if node is None:
                continue
            if node.is_visible is False:
                continue
            elements.append(_element_summary(node, idx))
        await browser_hub.sync_tab_metadata(tab_id, session)
        rec = browser_hub.get_tab_record(tab_id)
        out: dict[str, Any] = {
            "success": True,
            "tab_id": tab_id,
            "url": rec.url if rec else None,
            "title": rec.title if rec else None,
            "elements": elements,
            "elements_returned": len(elements),
            "elements_truncated": len(selector_map) > max_n,
            "hint": "Use element index with browser_click / browser_type, or pass css_selector.",
        }
        if state.page_info:
            pi = state.page_info
            out["viewport"] = {
                "width": pi.viewport_width,
                "height": pi.viewport_height,
                "scroll_x": pi.scroll_x,
                "scroll_y": pi.scroll_y,
            }
        return out
    except Exception as e:
        logger.warning("browser_get_page_state %s: %s", tab_id, e)
        return {"error": "page_state_failed", "detail": type(e).__name__, "tab_id": tab_id}


async def click(
    tab_id: str,
    *,
    element_index: int | None = None,
    css_selector: str = "",
    x: float | None = None,
    y: float | None = None,
) -> dict[str, Any]:
    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id}
    try:
        if x is not None and y is not None:
            from browser_use.browser.events import ClickCoordinateEvent

            await session.event_bus.dispatch(
                ClickCoordinateEvent(coordinate_x=float(x), coordinate_y=float(y))
            )
        elif css_selector.strip():
            pw_err = await _click_css_selector(tab_id, css_selector.strip(), session)
            if pw_err:
                return {"error": pw_err, "tab_id": tab_id}
        elif element_index is not None:
            from browser_use.browser.events import ClickElementEvent

            node = await session.get_element_by_index(int(element_index))
            if node is None:
                return {
                    "error": "element_index_not_found",
                    "tab_id": tab_id,
                    "hint": "Call browser_get_page_state again after navigation.",
                }
            await session.event_bus.dispatch(ClickElementEvent(node=node))
        else:
            return {
                "error": "specify_element_index_css_selector_or_coordinates",
                "tab_id": tab_id,
            }
        await browser_hub.sync_tab_metadata(tab_id, session)
        return {"success": True, "tab_id": tab_id}
    except Exception as e:
        logger.warning("browser_click %s: %s", tab_id, e)
        return {"error": "click_failed", "detail": type(e).__name__, "tab_id": tab_id}


async def type_text(
    tab_id: str,
    text: str = "",
    *,
    element_index: int | None = None,
    css_selector: str = "",
    secret_name: str = "",
    clear_first: bool = True,
) -> dict[str, Any]:
    value = (text or "").strip()
    if secret_name.strip():
        import secrets_store

        if not secrets_store.master_key_configured():
            return {"error": "secrets_not_configured", "tab_id": tab_id}
        got = secrets_store.get_secret(secret_name.strip())
        if got is None:
            return {"error": "unknown_or_missing_secret", "secret_name": secret_name.strip(), "tab_id": tab_id}
        value = got
    if not value:
        return {"error": "text_or_secret_name_required", "tab_id": tab_id}

    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id}
    try:
        if css_selector.strip():
            pw_err = await _type_css_selector(
                tab_id, css_selector.strip(), value, clear_first=clear_first
            )
            if pw_err:
                return {"error": pw_err, "tab_id": tab_id}
        elif element_index is not None:
            from browser_use.browser.events import ClickElementEvent, TypeTextEvent

            node = await session.get_element_by_index(int(element_index))
            if node is None:
                return {"error": "element_index_not_found", "tab_id": tab_id}
            await session.event_bus.dispatch(ClickElementEvent(node=node))
            await session.event_bus.dispatch(
                TypeTextEvent(node=node, text=value, clear=clear_first)
            )
        else:
            return {"error": "specify_element_index_or_css_selector", "tab_id": tab_id}
        await browser_hub.sync_tab_metadata(tab_id, session)
        return {"success": True, "tab_id": tab_id, "typed_chars": len(value)}
    except Exception as e:
        logger.warning("browser_type %s: %s", tab_id, e)
        return {"error": "type_failed", "detail": type(e).__name__, "tab_id": tab_id}


async def press_keys(tab_id: str, keys: str) -> dict[str, Any]:
    if not (keys or "").strip():
        return {"error": "keys_required", "tab_id": tab_id}
    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id}
    try:
        from browser_use.browser.events import SendKeysEvent

        await session.event_bus.dispatch(SendKeysEvent(keys=keys.strip()))
        await browser_hub.sync_tab_metadata(tab_id, session)
        return {"success": True, "tab_id": tab_id, "keys": keys.strip()}
    except Exception as e:
        logger.warning("browser_press_keys %s: %s", tab_id, e)
        return {"error": "press_keys_failed", "detail": type(e).__name__, "tab_id": tab_id}


async def _click_css_selector(tab_id: str, selector: str, session: Any) -> str | None:
    """Playwright fill path for CSS selectors (hub CDP)."""
    try:
        from playwright.async_api import async_playwright

        cdp = browser_hub.hub_cdp_url()
        if not cdp:
            return "browser_hub_inactive"
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp)
            try:
                page = await _playwright_page_for_tab(browser, tab_id)
                if page is None:
                    return "no_playwright_page_for_tab"
                await page.click(selector, timeout=30000)
            finally:
                await browser.close()
        return None
    except Exception as e:
        return f"css_click_failed:{type(e).__name__}"


async def _type_css_selector(
    tab_id: str, selector: str, text: str, *, clear_first: bool
) -> str | None:
    try:
        from playwright.async_api import async_playwright

        cdp = browser_hub.hub_cdp_url()
        if not cdp:
            return "browser_hub_inactive"
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp)
            try:
                page = await _playwright_page_for_tab(browser, tab_id)
                if page is None:
                    return "no_playwright_page_for_tab"
                if clear_first:
                    await page.fill(selector, text, timeout=30000)
                else:
                    await page.focus(selector, timeout=30000)
                    await page.keyboard.insert_text(text)
            finally:
                await browser.close()
        return None
    except Exception as e:
        return f"css_type_failed:{type(e).__name__}"


async def _playwright_page_for_tab(browser: Any, tab_id: str) -> Any | None:
    target_id = browser_hub.tab_target_id(tab_id)
    if not browser.contexts:
        return None
    ctx = browser.contexts[0]
    pages = list(ctx.pages)
    if not pages:
        return None
    if target_id:
        for pg in pages:
            try:
                if pg._impl_obj._target_id == target_id:  # type: ignore[attr-defined]
                    return pg
            except Exception:
                continue
    rec = browser_hub.get_tab_record(tab_id)
    if rec and rec.url:
        for pg in pages:
            if pg.url == rec.url:
                return pg
    return pages[-1]
