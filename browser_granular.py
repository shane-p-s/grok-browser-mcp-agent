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


def _visible_text_max_items() -> int:
    try:
        return max(10, min(int(os.getenv("BROWSER_VISIBLE_TEXT_MAX_ITEMS", "60")), 120))
    except ValueError:
        return 60


def _element_summary(node: Any, index: int) -> dict[str, Any]:
    attrs = node.attributes or {}
    text = (node.node_value or "").strip()
    for key in ("aria-label", "title", "alt", "value", "placeholder"):
        if text:
            break
        raw = attrs.get(key)
        if raw:
            text = str(raw).strip()
    if not text and attrs.get("aria-labelledby"):
        text = f"aria-labelledby={attrs.get('aria-labelledby')}"
    if len(text) > 160:
        text = text[:160] + "…"
    cls = attrs.get("class")
    if isinstance(cls, list):
        cls = " ".join(str(c) for c in cls)
    cls_s = (str(cls).strip()[:80] if cls else None) or None
    return {
        "index": index,
        "tag": (node.node_name or "").lower(),
        "text": text or None,
        "role": attrs.get("role"),
        "id": attrs.get("id"),
        "name": attrs.get("name"),
        "type": attrs.get("type"),
        "placeholder": attrs.get("placeholder"),
        "href": attrs.get("href"),
        "data_testid": attrs.get("data-testid"),
        "class": cls_s,
    }


def _element_sort_key(el: dict[str, Any]) -> tuple[int, int]:
    """Prefer elements with text/labels for SPA usefulness."""
    has_text = 1 if el.get("text") else 0
    has_id = 1 if el.get("id") or el.get("data_testid") else 0
    return (has_text, has_id)


_VISIBLE_REGIONS_JS = """
() => {
  const sel = [
    'a[href]', 'button', 'input', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]',
    '[aria-label]', '[data-testid]', 'h1', 'h2', 'h3', 'h4', 'label', 'p', 'span', 'li'
  ].join(',');
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
    if (!text && !el.getAttribute('data-testid') && !el.id && el.tagName !== 'INPUT') continue;
    const tag = el.tagName.toLowerCase();
    const snippet = text.length > 140 ? text.slice(0, 140) + '…' : text;
    out.push({
      tag,
      text: snippet || null,
      role: el.getAttribute('role'),
      id: el.id || null,
      data_testid: el.getAttribute('data-testid'),
      href: el.href || null,
      center_x: Math.round(r.left + r.width / 2),
      center_y: Math.round(r.top + r.height / 2),
    });
    seen.add(el);
    if (out.length >= MAX_ITEMS) break;
  }
  return out;
}
"""


async def _fetch_visible_regions(tab_id: str, max_items: int) -> tuple[list[dict[str, Any]], str | None]:
    """DOM innerText / aria snapshot for JS-heavy SPAs (Playwright evaluate)."""
    try:
        from playwright.async_api import async_playwright

        cdp = browser_hub.hub_cdp_url()
        if not cdp:
            return [], "browser_hub_inactive_or_cdp_lost"
        script = _VISIBLE_REGIONS_JS.replace("MAX_ITEMS", str(max_items))
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp)
            try:
                page = await _playwright_page_for_tab(browser, tab_id)
                if page is None:
                    return [], "no_playwright_page_for_tab"
                regions = await page.evaluate(script)
            finally:
                await browser.close()
        if not isinstance(regions, list):
            return [], "visible_regions_unexpected_shape"
        return regions, None
    except Exception as e:
        logger.warning("visible_regions %s: %s", tab_id, e)
        return [], f"visible_regions_failed:{type(e).__name__}"


def _hub_recovery_fields(err: str | None) -> dict[str, Any]:
    if err in (
        "browser_hub_inactive_or_cdp_lost",
        "tab_stale_hub_disconnected",
        "browser_hub_inactive",
    ):
        return {"hub_recovery_hint": browser_hub.HUB_RECOVERY_HINT}
    return {}


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
        return {"error": err, "tab_id": tab_id, **_hub_recovery_fields(err)}
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


async def get_page_state(
    tab_id: str,
    *,
    light: bool = False,
    include_visible_text: bool = True,
) -> dict[str, Any]:
    """
    light=true: skip heavy selector_map walk (faster; use with Watch Mode + visible_regions).
    include_visible_text=true: add visible_regions (innerText/aria) for React/SPA sites.
    """
    max_visible = _visible_text_max_items()
    rec0 = browser_hub.get_tab_record(tab_id)
    if not rec0 or rec0.status == "closed":
        return {"error": "tab_not_found_or_closed", "tab_id": tab_id}
    if rec0.stale:
        return {
            "error": "tab_stale_hub_disconnected",
            "tab_id": tab_id,
            "hub_recovery_hint": browser_hub.HUB_RECOVERY_HINT,
        }
    if light:
        regions, vr_err = await _fetch_visible_regions(tab_id, max_visible)
        rec = browser_hub.get_tab_record(tab_id)
        if vr_err and vr_err not in ("no_playwright_page_for_tab",):
            err_code = vr_err if vr_err.startswith("browser_") or vr_err.startswith("tab_") else "page_state_failed"
            if err_code == "page_state_failed":
                return {"error": err_code, "detail": vr_err, "tab_id": tab_id}
            return {"error": err_code, "tab_id": tab_id, **_hub_recovery_fields(err_code)}
        out: dict[str, Any] = {
            "success": True,
            "tab_id": tab_id,
            "url": rec.url if rec else None,
            "title": rec.title if rec else None,
            "light": True,
            "visible_regions": regions,
            "visible_regions_count": len(regions),
            "hint": (
                "SPA/light mode: prefer visible_regions.center_x/center_y with browser_click(x=, y=, return_screenshot=false) "
                "or Watch Mode (browser_watch_start). element index list omitted in light mode."
            ),
        }
        if vr_err:
            out["visible_regions_note"] = vr_err
        return out

    session, err = await browser_hub.attach_session_for_tab(tab_id)
    if err:
        return {"error": err, "tab_id": tab_id, **_hub_recovery_fields(err)}
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
        for idx in sorted(selector_map.keys()):
            node = selector_map[idx]
            if node is None:
                continue
            if node.is_visible is False:
                continue
            elements.append(_element_summary(node, idx))
        elements.sort(key=_element_sort_key, reverse=True)
        elements = elements[:max_n]
        textful = sum(1 for e in elements if e.get("text"))
        await browser_hub.sync_tab_metadata(tab_id, session)
        rec = browser_hub.get_tab_record(tab_id)
        out = {
            "success": True,
            "tab_id": tab_id,
            "url": rec.url if rec else None,
            "title": rec.title if rec else None,
            "elements": elements,
            "elements_returned": len(elements),
            "elements_with_text": textful,
            "elements_truncated": len(selector_map) > max_n,
            "hint": (
                "If elements lack text (common on React SPAs), use include_visible_text=true, light=true, "
                "or Watch Mode + browser_click(x, y, return_screenshot=false)."
            ),
        }
        if state.page_info:
            pi = state.page_info
            out["viewport"] = {
                "width": pi.viewport_width,
                "height": pi.viewport_height,
                "scroll_x": pi.scroll_x,
                "scroll_y": pi.scroll_y,
            }
        if include_visible_text:
            regions, vr_err = await _fetch_visible_regions(tab_id, max_visible)
            out["visible_regions"] = regions
            out["visible_regions_count"] = len(regions)
            if vr_err:
                out["visible_regions_note"] = vr_err
        if textful < 3 and include_visible_text and out.get("visible_regions_count", 0) > 0:
            out["spa_detected"] = True
            out["recommended"] = "Use visible_regions.center_x/center_y or browser_watch_start for vision."
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
        return {"error": err, "tab_id": tab_id, **_hub_recovery_fields(err)}
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
        return {"error": err, "tab_id": tab_id, **_hub_recovery_fields(err)}
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
        return {"error": err, "tab_id": tab_id, **_hub_recovery_fields(err)}
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
