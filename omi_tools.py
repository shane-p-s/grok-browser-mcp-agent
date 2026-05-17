"""Omi wearable integration: conversation-first recall + local Chroma index."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

import secrets_store
import tool_gating

logger = logging.getLogger(__name__)

OMI_API_BASE = (os.getenv("OMI_API_BASE") or "https://api.omi.me/v1/dev").rstrip("/")
OMI_SECRET_NAME = "omi_api_key"
_DEFAULT_DAYS = int(os.getenv("OMI_RECALL_DEFAULT_DAYS", "14"))
_MAX_RESPONSE_CHARS = int(os.getenv("OMI_MAX_RESPONSE_CHARS", "32000"))
_MAX_EXCERPT_CHARS = int(os.getenv("OMI_MAX_EXCERPT_CHARS", "8000"))
_SYNC_DEFAULT_DAYS = int(os.getenv("OMI_SYNC_DEFAULT_DAYS", "30"))
_SYNC_INTERVAL_HOURS = float(os.getenv("OMI_SYNC_INTERVAL_HOURS", "6"))
_INDEX_READY_MAX_AGE_HOURS = float(os.getenv("OMI_INDEX_READY_MAX_AGE_HOURS", "24"))
_SYNC_PAGE_DELAY = float(os.getenv("OMI_SYNC_PAGE_DELAY_SECONDS", "0.65"))
_CHROMA_SUBDIR = "omi_chroma"
_SYNC_STATE_FILE = "omi_sync_state.json"

_chroma_init_lock = asyncio.Lock()
_background_sync_task: asyncio.Task[None] | None = None

_PRECISION_PATTERNS = re.compile(
    r"\b(did i say|exactly|quote|verbatim|commit|agree|promise|deadline|"
    r"what did i say|are you sure|when did|how much|which day)\b",
    re.I,
)


def _data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "grok-mcp-agent"


def _chroma_path() -> Path:
    raw = (os.getenv("OMI_CHROMA_PATH") or "").strip()
    return Path(raw) if raw else _data_dir() / _CHROMA_SUBDIR


def _sync_state_path() -> Path:
    return _data_dir() / _SYNC_STATE_FILE


def omi_api_key_configured() -> bool:
    if not secrets_store.master_key_configured():
        return False
    if secrets_store.get_secret(OMI_SECRET_NAME):
        return True
    return any(m.get("name") == OMI_SECRET_NAME for m in secrets_store.list_secret_metadata())


def _api_key() -> str | None:
    return secrets_store.get_secret(OMI_SECRET_NAME)


def _api_key_error() -> dict[str, Any]:
    return {
        "error": "omi_not_configured",
        "hint": f'Set secret via request_user_secret(name="{OMI_SECRET_NAME}") after SECRETS_MASTER_KEY is configured.',
    }


def _load_sync_state() -> dict[str, Any]:
    p = _sync_state_path()
    if not p.is_file():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("omi sync state load failed: %s", e)
        return {}


def _save_sync_state(data: dict[str, Any]) -> None:
    p = _sync_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(p)


def index_ready() -> bool:
    st = _load_sync_state()
    last = st.get("last_sync_at")
    counts = st.get("counts") or {}
    total = int(counts.get("memories", 0)) + int(counts.get("conversations", 0))
    if not last or total <= 0:
        return False
    try:
        ts = float(last)
    except (TypeError, ValueError):
        return False
    age_h = (time.time() - ts) / 3600.0
    return age_h <= _INDEX_READY_MAX_AGE_HOURS


def index_stale() -> bool:
    return omi_api_key_configured() and not index_ready()


def _iso_start(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_terms(query: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]{2,}", (query or "").lower())
    stop = {
        "the", "and", "for", "what", "did", "say", "about", "with", "from", "that",
        "this", "have", "been", "were", "when", "where", "your", "my", "me", "i",
        "recent", "life", "context", "last", "week", "day", "tell",
    }
    return [t for t in raw if t not in stop][:12]


def _score_text(text: str, terms: list[str]) -> float:
    if not text or not terms:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in terms if t in low)
    return hits / len(terms)


def _needs_excerpts(query: str, conversations: list[dict[str, Any]], memories: list[dict[str, Any]]) -> bool:
    if _PRECISION_PATTERNS.search(query or ""):
        return True
    terms = _query_terms(query)
    if not terms:
        return False
    blob_parts: list[str] = []
    for c in conversations[:3]:
        blob_parts.append(str(c.get("overview") or ""))
        blob_parts.append(str(c.get("title") or ""))
    for m in memories[:5]:
        blob_parts.append(str(m.get("content") or ""))
    blob = " ".join(blob_parts)
    return _score_text(blob, terms) < 0.35


def _conv_doc_for_index(conv: dict[str, Any]) -> str:
    st = conv.get("structured") or {}
    parts = [
        str(st.get("title") or ""),
        str(st.get("overview") or ""),
        str(conv.get("folder_name") or ""),
    ]
    geo = conv.get("geolocation") or {}
    if isinstance(geo, dict) and geo.get("address"):
        parts.append(str(geo["address"]))
    return "\n".join(p for p in parts if p).strip()


def _memory_doc(mem: dict[str, Any]) -> str:
    return str(mem.get("content") or "").strip()


def _slim_conversation(conv: dict[str, Any], terms: list[str], confidence: str) -> dict[str, Any]:
    st = conv.get("structured") or {}
    overview = str(st.get("overview") or "")
    title = str(st.get("title") or "")
    when = conv.get("started_at") or conv.get("created_at")
    snippet = overview[:400]
    if terms:
        for t in terms:
            idx = overview.lower().find(t)
            if idx >= 0:
                start = max(0, idx - 80)
                snippet = overview[start : start + 280]
                break
    return {
        "id": conv.get("id"),
        "type": "conversation",
        "title": title or None,
        "overview": overview or None,
        "when": when,
        "folder": conv.get("folder_name"),
        "category": st.get("category"),
        "relevance_snippet": snippet,
        "confidence": confidence,
    }


def _slim_memory(mem: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    content = str(mem.get("content") or "")
    conf = "high" if terms and _score_text(content, terms) >= 0.5 else "medium"
    return {
        "id": mem.get("id"),
        "type": "memory",
        "content": content,
        "category": mem.get("category"),
        "tags": mem.get("tags") or [],
        "when": mem.get("updated_at") or mem.get("created_at"),
        "relevance_snippet": content[:300],
        "confidence": conf,
    }


def _slim_action_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "description": item.get("description"),
        "completed": item.get("completed"),
        "due_at": item.get("due_at"),
        "conversation_id": item.get("conversation_id"),
        "created_at": item.get("created_at"),
    }


def _extract_excerpts(
    segments: list[dict[str, Any]],
    query: str,
    max_chars: int,
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not segments:
        return []
    scored: list[tuple[float, int]] = []
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        scored.append((_score_text(text, terms) if terms else 0.1, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[dict[str, Any]] = []
    used_chars = 0
    used_idx: set[int] = set()
    for _score, idx in scored[:8]:
        if idx in used_idx:
            continue
        window: list[dict[str, Any]] = []
        for j in range(max(0, idx - 2), min(len(segments), idx + 3)):
            if j in used_idx:
                continue
            seg = segments[j]
            window.append(
                {
                    "speaker_name": seg.get("speaker_name"),
                    "start": seg.get("start"),
                    "end": seg.get("end"),
                    "text": seg.get("text"),
                }
            )
            used_idx.add(j)
        block = json.dumps(window, ensure_ascii=False)
        if used_chars + len(block) > max_chars:
            break
        out.extend(window)
        used_chars += len(block)
        if len(out) >= 12:
            break
    return out


def _cap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= _MAX_RESPONSE_CHARS:
        return payload
    payload = dict(payload)
    payload["truncated"] = True
    for key in ("transcript_excerpts", "conversations", "memories", "action_items"):
        arr = payload.get(key)
        if isinstance(arr, list) and len(arr) > 2:
            payload[key] = arr[:2]
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > _MAX_RESPONSE_CHARS:
        payload["answer_hints"] = (payload.get("answer_hints") or [])[:2]
    return payload


async def _omi_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any] | list[Any]:
    key = _api_key()
    if not key:
        raise RuntimeError("omi_api_key not configured")
    url = f"{OMI_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "grok-browser-mcp-agent"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, url, headers=headers, params=params, json=json_body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": (r.text or "")[:4000]}
        if r.status_code >= 400:
            return {"error": "omi_api_error", "status_code": r.status_code, "body": data}
        return data


def _as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and "error" in data:
        return []
    if isinstance(data, dict):
        return [data]
    return []


# --- Chroma (optional at import; required for sync) ---

_chroma_client: Any = None
_mem_col: Any = None
_conv_col: Any = None


def _chroma_available() -> bool:
    try:
        import chromadb  # noqa: F401

        return True
    except ImportError:
        return False


async def _ensure_chroma() -> tuple[Any, Any, Any] | None:
    global _chroma_client, _mem_col, _conv_col
    if not _chroma_available():
        return None
    async with _chroma_init_lock:
        if _chroma_client is not None:
            return _chroma_client, _mem_col, _conv_col

        def _init() -> tuple[Any, Any, Any]:
            import chromadb

            path = _chroma_path()
            path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(path))
            mem = client.get_or_create_collection(name="omi_memories", metadata={"hnsw:space": "cosine"})
            conv = client.get_or_create_collection(name="omi_conversations", metadata={"hnsw:space": "cosine"})
            return client, mem, conv

        _chroma_client, _mem_col, _conv_col = await asyncio.to_thread(_init)
        return _chroma_client, _mem_col, _conv_col


async def _chroma_query_collection(
    collection: Any,
    query: str,
    n: int,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": n, "include": ["documents", "metadatas", "distances"]}
        if where:
            kwargs["where"] = where
        res = collection.query(**kwargs)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict[str, Any]] = []
        for doc, meta, dist in zip(docs, metas, dists):
            out.append({"document": doc, "metadata": meta or {}, "distance": dist})
        return out

    return await asyncio.to_thread(_run)


async def omi_ping() -> dict[str, Any]:
    if not omi_api_key_configured():
        return {**_api_key_error(), "ok": False}
    try:
        data = await _omi_request("GET", "/user/memories", params={"limit": 1})
        if isinstance(data, dict) and data.get("error"):
            return {"ok": False, **data}
        return {"ok": True, "message": "Omi API reachable", "index_ready": index_ready()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def omi_remember(
    content: str,
    category: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    if not omi_api_key_configured():
        return _api_key_error()
    text = (content or "").strip()
    if not text:
        return {"error": "content is required"}
    if len(text) > 500:
        text = text[:500]
    body: dict[str, Any] = {"content": text}
    if category:
        body["category"] = category.strip()
    if tags:
        body["tags"] = [str(t) for t in tags[:20]]
    data = await _omi_request("POST", "/user/memories", json_body=body)
    if isinstance(data, dict) and data.get("error"):
        return data
    return {"ok": True, "memory": data}


async def _list_memories_page(limit: int, offset: int, categories: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if categories:
        params["categories"] = categories
    data = await _omi_request("GET", "/user/memories", params=params)
    return _as_list(data)


async def _list_conversations_page(
    *,
    start_date: str,
    limit: int,
    offset: int,
    include_transcript: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
        "start_date": start_date,
        "include_transcript": str(include_transcript).lower(),
    }
    data = await _omi_request("GET", "/user/conversations", params=params)
    return _as_list(data)


async def _get_conversation(conversation_id: str, *, include_transcript: bool = True) -> dict[str, Any]:
    cid = (conversation_id or "").strip()
    if not cid:
        return {"error": "conversation_id required"}
    params = {"include_transcript": str(include_transcript).lower()}
    data = await _omi_request("GET", f"/user/conversations/{cid}", params=params)
    if isinstance(data, dict) and not data.get("error"):
        return data
    return data if isinstance(data, dict) else {"error": "unexpected_response"}


async def _list_open_action_items(limit: int = 50) -> list[dict[str, Any]]:
    data = await _omi_request(
        "GET",
        "/user/action-items",
        params={"completed": "false", "limit": limit},
    )
    return _as_list(data)


async def _recall_via_api(
    query: str,
    days: int,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    terms = _query_terms(query)
    start = _iso_start(days)
    mem_task = _list_memories_page(min(100, limit * 8), 0)
    conv_task = _list_conversations_page(start_date=start, limit=min(50, limit * 10), offset=0)
    actions_task = _list_open_action_items(50)
    mem_raw, conv_raw, actions_raw = await asyncio.gather(mem_task, conv_task, actions_task)

    mem_scored = sorted(
        ((_score_text(_memory_doc(m), terms), m) for m in mem_raw),
        key=lambda x: -x[0],
    )
    memories = [_slim_memory(m, terms) for _s, m in mem_scored[:limit] if _s > 0 or not terms][:limit]

    conv_scored = sorted(
        ((_score_text(_conv_doc_for_index(c), terms), c) for c in conv_raw),
        key=lambda x: -x[0],
    )
    conversations = [
        _slim_conversation(c, terms, "high" if s >= 0.5 else "medium")
        for s, c in conv_scored[:limit]
        if s > 0 or not terms
    ][:limit]

    action_scored = sorted(
        ((_score_text(str(a.get("description") or ""), terms), a) for a in actions_raw),
        key=lambda x: -x[0],
    )
    action_items = [_slim_action_item(a) for _s, a in action_scored[: min(15, limit * 2)]]

    return memories, conversations, action_items


async def _recall_via_chroma(
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cols = await _ensure_chroma()
    if cols is None:
        return [], []
    _client, mem_col, conv_col = cols
    mem_hits, conv_hits = await asyncio.gather(
        _chroma_query_collection(mem_col, query, limit),
        _chroma_query_collection(conv_col, query, limit),
    )
    terms = _query_terms(query)
    memories: list[dict[str, Any]] = []
    for h in mem_hits:
        meta = h.get("metadata") or {}
        mem = {
            "id": meta.get("omi_id"),
            "content": h.get("document") or meta.get("content"),
            "category": meta.get("category"),
            "tags": json.loads(meta["tags"]) if meta.get("tags") else [],
            "updated_at": meta.get("updated_at"),
            "created_at": meta.get("created_at"),
        }
        memories.append(_slim_memory(mem, terms))

    conversations: list[dict[str, Any]] = []
    for h in conv_hits:
        meta = h.get("metadata") or {}
        st = {
            "title": meta.get("title"),
            "overview": meta.get("overview") or h.get("document"),
            "category": meta.get("category"),
        }
        conv = {
            "id": meta.get("omi_id"),
            "structured": st,
            "started_at": meta.get("started_at"),
            "created_at": meta.get("created_at"),
            "folder_name": meta.get("folder_name"),
        }
        conversations.append(_slim_conversation(conv, terms, "medium"))

    return memories, conversations


async def omi_recall(
    query: str,
    days: int = _DEFAULT_DAYS,
    depth: Literal["auto", "summary", "excerpts", "full"] = "auto",
    limit: int = 5,
) -> dict[str, Any]:
    """
    Primary Omi read: summaries + memories + open tasks; auto-fetches transcript excerpts when needed.
  Grok should pass the user's intent in plain language as query. Prefer this over list/get chains.
    """
    if not omi_api_key_configured():
        return _api_key_error()
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    lim = max(1, min(limit, 10))
    days = max(1, min(days, 365))
    depth_used: str = "summary"

    memories: list[dict[str, Any]] = []
    conversations: list[dict[str, Any]] = []
    action_items: list[dict[str, Any]] = []

    if index_ready() and _chroma_available():
        memories, conversations = await _recall_via_chroma(q, lim)
        action_items = [_slim_action_item(a) for a in await _list_open_action_items(30)]
        if not conversations and not memories:
            memories, conversations, action_items = await _recall_via_api(q, days, lim)
    else:
        memories, conversations, action_items = await _recall_via_api(q, days, lim)

    answer_hints: list[str] = []
    if conversations:
        top = conversations[0]
        if top.get("overview"):
            answer_hints.append(f"Lead with: {top.get('title') or 'Recent conversation'} — {str(top['overview'])[:200]}")
    open_tasks = [a for a in action_items if not a.get("completed")]
    if open_tasks:
        answer_hints.append(f"Open task: {open_tasks[0].get('description', '')[:120]}")

    transcript_excerpts: list[dict[str, Any]] = []
    full_transcript: dict[str, Any] | None = None

    want_excerpts = depth in ("excerpts", "full") or (
        depth == "auto" and _needs_excerpts(q, conversations, memories)
    )
    want_full = depth == "full" or (depth == "auto" and want_excerpts and _PRECISION_PATTERNS.search(q))

    if want_excerpts and conversations:
        depth_used = "excerpts"
        for conv in conversations[:3]:
            cid = conv.get("id")
            if not cid:
                continue
            detail = await _get_conversation(str(cid), include_transcript=True)
            if detail.get("error"):
                continue
            segments = detail.get("transcript_segments") or []
            if not isinstance(segments, list):
                segments = []
            ex = _extract_excerpts(segments, q, _MAX_EXCERPT_CHARS)
            if ex:
                transcript_excerpts.append({"conversation_id": cid, "title": conv.get("title"), "segments": ex})
        if not transcript_excerpts and conversations and depth == "auto":
            want_full = True

    if want_full and conversations:
        depth_used = "full"
        cid = str(conversations[0].get("id") or "")
        if cid:
            detail = await _get_conversation(cid, include_transcript=True)
            if not detail.get("error"):
                segments = detail.get("transcript_segments") or []
                full_transcript = {
                    "conversation_id": cid,
                    "title": (detail.get("structured") or {}).get("title"),
                    "segment_count": len(segments) if isinstance(segments, list) else 0,
                    "transcript_segments": segments[:200] if isinstance(segments, list) else [],
                }

    payload: dict[str, Any] = {
        "query": q,
        "days": days,
        "answer_hints": answer_hints,
        "memories": memories,
        "conversations": conversations,
        "action_items": action_items,
        "transcript_excerpts": transcript_excerpts,
        "_meta": {
            "source": "omi",
            "depth_used": depth_used,
            "index_ready": index_ready(),
            "index_stale": index_stale(),
            "reliability_note": "Generally accurate; verify precise quotes from transcript excerpts when stating commitments or exact wording.",
        },
    }
    if full_transcript:
        payload["full_transcript"] = full_transcript
    if index_stale():
        schedule_background_omi_sync()
    return _cap_payload(payload)


async def omi_sync_index(days: int = _SYNC_DEFAULT_DAYS, full: bool = False) -> dict[str, Any]:
    """Pull recent Omi data into local Chroma index for fast omi_recall."""
    if not omi_api_key_configured():
        return _api_key_error()
    if not _chroma_available():
        return {"error": "chromadb_not_installed", "hint": "pip install chromadb>=0.5.0"}
    cols = await _ensure_chroma()
    if cols is None:
        return {"error": "chroma_init_failed"}
    _client, mem_col, conv_col = cols
    days = max(1, min(days, 365))
    start = _iso_start(days)
    mem_count = 0
    conv_count = 0
    offset = 0
    page_size = 50

    while True:
        page = await _list_memories_page(page_size, offset)
        if not page:
            break

        def _upsert_mem(batch: list[dict[str, Any]]) -> None:
            ids = []
            docs = []
            metas = []
            for m in batch:
                mid = str(m.get("id") or "")
                if not mid:
                    continue
                doc = _memory_doc(m)
                if not doc:
                    continue
                ids.append(mid)
                docs.append(doc)
                metas.append(
                    {
                        "omi_id": mid,
                        "category": str(m.get("category") or ""),
                        "created_at": str(m.get("created_at") or ""),
                        "updated_at": str(m.get("updated_at") or ""),
                        "content": doc[:500],
                        "tags": json.dumps(m.get("tags") or []),
                    }
                )
            if ids:
                mem_col.upsert(ids=ids, documents=docs, metadatas=metas)

        await asyncio.to_thread(_upsert_mem, page)
        mem_count += len(page)
        if len(page) < page_size:
            break
        offset += page_size
        await asyncio.sleep(_SYNC_PAGE_DELAY)

    offset = 0
    while True:
        page = await _list_conversations_page(start_date=start, limit=page_size, offset=offset)
        if not page:
            break

        def _upsert_conv(batch: list[dict[str, Any]]) -> None:
            ids = []
            docs = []
            metas = []
            for c in batch:
                cid = str(c.get("id") or "")
                doc = _conv_doc_for_index(c)
                if not cid or not doc:
                    continue
                st = c.get("structured") or {}
                ids.append(cid)
                docs.append(doc)
                metas.append(
                    {
                        "omi_id": cid,
                        "title": str(st.get("title") or ""),
                        "overview": str(st.get("overview") or "")[:1000],
                        "category": str(st.get("category") or ""),
                        "started_at": str(c.get("started_at") or ""),
                        "created_at": str(c.get("created_at") or ""),
                        "folder_name": str(c.get("folder_name") or ""),
                    }
                )
            if ids:
                conv_col.upsert(ids=ids, documents=docs, metadatas=metas)

        await asyncio.to_thread(_upsert_conv, page)
        conv_count += len(page)
        if len(page) < page_size:
            break
        offset += page_size
        await asyncio.sleep(_SYNC_PAGE_DELAY)

    state = {
        "last_sync_at": time.time(),
        "last_sync_days": days,
        "full": full,
        "counts": {"memories": mem_count, "conversations": conv_count},
    }
    _save_sync_state(state)
    return {"ok": True, "indexed": state["counts"], "index_ready": True}


async def _background_sync_once() -> None:
    if not omi_api_key_configured() or not _chroma_available():
        return
    st = _load_sync_state()
    last = st.get("last_sync_at")
    if last:
        try:
            age_h = (time.time() - float(last)) / 3600.0
            if age_h < _SYNC_INTERVAL_HOURS:
                return
        except (TypeError, ValueError):
            pass
    try:
        result = await omi_sync_index(days=_SYNC_DEFAULT_DAYS, full=False)
        logger.info("omi background sync: %s", result.get("indexed") or result.get("error"))
    except Exception as e:
        logger.warning("omi background sync failed: %s", e)


def schedule_background_omi_sync() -> None:
    """Fire-and-forget warm index sync (call from app lifespan)."""
    global _background_sync_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _background_sync_task and not _background_sync_task.done():
        return

    async def _runner() -> None:
        await asyncio.sleep(3)
        await _background_sync_once()

    _background_sync_task = loop.create_task(_runner())


def register_omi_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def omi_ping_tool() -> dict[str, Any]:
        """Check Omi API connectivity and whether the local recall index is warm."""
        if (g := tool_gating.tool_disabled_error("omi_ping")) is not None:
            return g
        return await omi_ping()

    @mcp.tool()
    async def omi_recall_tool(
        query: str,
        days: int = _DEFAULT_DAYS,
        depth: str = "auto",
        limit: int = 5,
    ) -> dict[str, Any]:
        """
        Primary Omi read for voice and chat. Pass the user's intent in plain language as query.
        Returns memories, conversation summaries, open action items; auto-fetches transcript excerpts
        when the query needs precision (quotes, did-I-say, names, numbers). Use once per turn in voice.
        depth: auto (default), summary, excerpts, or full.
        """
        if (g := tool_gating.tool_disabled_error("omi_recall")) is not None:
            return g
        d = (depth or "auto").strip().lower()
        if d not in ("auto", "summary", "excerpts", "full"):
            return {"error": "invalid depth", "allowed": ["auto", "summary", "excerpts", "full"]}
        return await omi_recall(query, days=days, depth=d, limit=limit)  # type: ignore[arg-type]

    @mcp.tool()
    async def omi_remember_tool(
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Store a durable fact in Omi when the user says remember / don't forget / keep in mind.
        Use timeless facts (preferences, relationships), not calendar tasks.
        """
        if (g := tool_gating.tool_disabled_error("omi_remember")) is not None:
            return g
        return await omi_remember(content, category=category, tags=tags)

    @mcp.tool()
    async def omi_sync_index_tool(days: int = _SYNC_DEFAULT_DAYS, full: bool = False) -> dict[str, Any]:
        """
        Refresh local semantic index for fast omi_recall. Runs automatically in the background on server start;
        call manually if index_stale or after long offline period.
        """
        if (g := tool_gating.tool_disabled_error("omi_sync_index")) is not None:
            return g
        return await omi_sync_index(days=days, full=full)

    @mcp.tool()
    async def omi_list_conversations_tool(
        start_date: str | None = None,
        days: int = 14,
        limit: int = 25,
        include_transcript: bool = False,
    ) -> dict[str, Any]:
        """Advanced: list conversations from Omi API. Prefer omi_recall for normal chat."""
        if (g := tool_gating.tool_disabled_error("omi_list_conversations")) is not None:
            return g
        if not omi_api_key_configured():
            return _api_key_error()
        sd = start_date or _iso_start(days)
        data = await _list_conversations_page(
            start_date=sd,
            limit=min(limit, 50),
            offset=0,
            include_transcript=include_transcript,
        )
        return {"conversations": data, "count": len(data)}

    @mcp.tool()
    async def omi_get_conversation_tool(
        conversation_id: str,
        include_transcript: bool = True,
    ) -> dict[str, Any]:
        """Advanced: fetch one conversation. Prefer omi_recall unless you have a specific id."""
        if (g := tool_gating.tool_disabled_error("omi_get_conversation")) is not None:
            return g
        if not omi_api_key_configured():
            return _api_key_error()
        return await _get_conversation(conversation_id, include_transcript=include_transcript)

    logger.info("Omi tools registered")
