"""GitHub REST helpers for MCP tools (read/list/diff at specific refs)."""

from __future__ import annotations

import base64
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

_GITHUB_MAX_DECODE_CHARS = int(os.getenv("GITHUB_GET_FILE_MAX_CHARS", "524288"))
_GITHUB_MAX_TREE_PATHS = int(os.getenv("GITHUB_LIST_FILES_MAX_PATHS", "8000"))
_GITHUB_MAX_DIFF_CHARS = int(os.getenv("GITHUB_GET_DIFF_MAX_CHARS", "200000"))


def safe_github_segment(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", s))


def safe_ref_param(ref: str) -> bool:
    """Branch, tag, or commit-ish: conservative allowlist."""
    return bool(re.fullmatch(r"[A-Za-z0-9._~^:/-]{1,200}", ref))


def safe_compare_ref(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._~^:/-]{1,200}", s))


async def github_request(
    method: str,
    url: str,
    token: str,
    json: dict | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "grok-browser-mcp-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers=headers, json=json)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:8000]}
            if r.status_code >= 400:
                return {"error": "github_api_error", "status_code": r.status_code, "body": data}
            return {"status_code": r.status_code, "data": data}
    except httpx.HTTPError as e:
        return {"error": str(e)}


def _process_get_file_payload(
    data: dict[str, Any],
    max_bytes: int | None,
) -> dict[str, Any]:
    """Mutate/extend successful Contents API JSON for a single object."""
    out = dict(data)
    t = data.get("type")
    if t == "symlink":
        out["hint"] = "Symlink target is in the `target` field; fetch the linked path if needed."
        return out
    if t == "submodule":
        out["hint"] = "Submodule pointer; clone the submodule repo or use its commit SHA separately."
        return out
    if t == "dir":
        out["hint"] = "Path is a directory; use github_list_repo_files for listing."
        return out
    if t != "file":
        return out
    enc = data.get("encoding")
    raw_b64 = data.get("content")
    if enc != "base64" or not isinstance(raw_b64, str):
        out["hint"] = "File entry has no base64 content (may be empty or large LFS pointer)."
        return out
    try:
        raw = base64.b64decode(raw_b64.replace("\n", ""), validate=False)
    except Exception as e:
        out["content_decode_error"] = str(e)[:200]
        return out
    limit = max_bytes if max_bytes is not None else _GITHUB_MAX_DECODE_CHARS
    limit = max(1024, min(limit, 2_000_000))
    truncated = len(raw) > limit
    chunk = raw[:limit]
    out["content_text"] = chunk.decode("utf-8", errors="replace")
    out["content_truncated"] = truncated
    out["content_byte_length"] = len(raw)
    return out


async def github_get_file_enriched(
    owner: str,
    repo: str,
    path: str,
    token: str,
    ref: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """
    GET /repos/{owner}/{repo}/contents/{path}?ref=...
    ref may be branch name, tag, or commit SHA.
    """
    if not safe_github_segment(owner) or not safe_github_segment(repo):
        return {"error": "invalid owner or repo"}
    path = path.lstrip("/")
    if ref is not None and ref.strip():
        r = ref.strip()
        if not safe_ref_param(r):
            return {"error": "invalid ref parameter"}
        q = f"?ref={quote(r, safe='')}"
    else:
        q = ""
    enc_path = "/".join(quote(seg, safe="") for seg in path.split("/") if seg) if path else ""
    base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    api_url = f"{base}/{enc_path}{q}" if enc_path else f"{base}{q}"
    res = await github_request("GET", api_url, token)
    if "error" in res:
        return res
    body = res.get("data")
    if isinstance(body, list):
        return {
            "status_code": res["status_code"],
            "data": body,
            "hint": "Path is a directory (array response); use github_list_repo_files for structured listing.",
        }
    if isinstance(body, dict):
        return {"status_code": res["status_code"], "data": _process_get_file_payload(body, max_bytes)}
    return res


async def github_list_repo_files(
    owner: str,
    repo: str,
    token: str,
    path: str = "",
    ref: str | None = None,
    recursive: bool = False,
) -> dict[str, Any]:
    """List files at ref: non-recursive uses Contents API; recursive uses Git Trees API."""
    if not safe_github_segment(owner) or not safe_github_segment(repo):
        return {"error": "invalid owner or repo"}
    path = path.lstrip("/")
    if ref is None or not ref.strip():
        return {"error": "ref is required for github_list_repo_files (branch, tag, or commit SHA)"}
    r = ref.strip()
    if not safe_ref_param(r):
        return {"error": "invalid ref parameter"}

    if not recursive:
        q = f"?ref={quote(r, safe='')}"
        enc_path = "/".join(quote(seg, safe="") for seg in path.split("/") if seg) if path else ""
        base = f"https://api.github.com/repos/{owner}/{repo}/contents"
        api_url = f"{base}/{enc_path}{q}" if enc_path else f"{base}{q}"
        res = await github_request("GET", api_url, token)
        if "error" in res:
            return res
        data = res.get("data")
        if isinstance(data, dict) and data.get("type") == "file":
            return {
                "status_code": res["status_code"],
                "error": "path_is_file",
                "hint": "Use github_get_file for file blobs.",
                "path": data.get("path"),
            }
        if not isinstance(data, list):
            return res
        entries = [{"name": x.get("name"), "path": x.get("path"), "type": x.get("type"), "sha": x.get("sha")} for x in data]
        return {
            "status_code": res["status_code"],
            "ref": r,
            "recursive": False,
            "entries": entries,
            "truncated": False,
        }

    commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{quote(r, safe='')}"
    cres = await github_request("GET", commit_url, token)
    if "error" in cres:
        return cres
    cdata = cres.get("data")
    if not isinstance(cdata, dict):
        return {"error": "unexpected_commits_response"}
    tree_sha = ((cdata.get("commit") or {}).get("tree") or {}).get("sha")
    if not tree_sha:
        tree_sha = cdata.get("sha")
    if not tree_sha:
        return {"error": "could_not_resolve_tree", "body_preview": str(cdata)[:500]}

    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1"
    tres = await github_request("GET", tree_url, token, timeout=90.0)
    if "error" in tres:
        return tres
    tdata = tres.get("data")
    if not isinstance(tdata, dict):
        return {"error": "unexpected_tree_response"}
    tree = tdata.get("tree") or []
    paths: list[str] = []
    truncated_api = bool(tdata.get("truncated"))
    for item in tree:
        if item.get("type") != "blob":
            continue
        p = item.get("path") or ""
        if path and not (p == path or p.startswith(path + "/")):
            continue
        paths.append(p)
        if len(paths) >= _GITHUB_MAX_TREE_PATHS:
            break
    return {
        "status_code": tres["status_code"],
        "ref": r,
        "recursive": True,
        "paths": paths,
        "truncated": truncated_api or len(paths) >= _GITHUB_MAX_TREE_PATHS,
        "path_filter": path or None,
    }


async def github_get_diff(
    owner: str,
    repo: str,
    base: str,
    head: str,
    token: str,
) -> dict[str, Any]:
    """GET compare API between base and head (SHAs, branches, or tags)."""
    if not safe_github_segment(owner) or not safe_github_segment(repo):
        return {"error": "invalid owner or repo"}
    if not safe_compare_ref(base) or not safe_compare_ref(head):
        return {"error": "invalid base or head"}
    spec = f"{base}...{head}"
    api_url = f"https://api.github.com/repos/{owner}/{repo}/compare/{spec}"
    res = await github_request("GET", api_url, token, timeout=90.0)
    if "error" in res:
        return res
    data = res.get("data")
    if not isinstance(data, dict):
        return res
    files_out: list[dict[str, Any]] = []
    patch_budget = _GITHUB_MAX_DIFF_CHARS
    for f in data.get("files") or []:
        if not isinstance(f, dict):
            continue
        patch = f.get("patch") or ""
        use = patch
        p_trunc = False
        if len(use) > min(32000, patch_budget):
            use = use[: min(32000, patch_budget)]
            p_trunc = True
        patch_budget -= len(use)
        files_out.append(
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "changes": f.get("changes"),
                "patch": use,
                "patch_truncated": p_trunc,
            }
        )
        if patch_budget <= 0:
            break
    all_files = data.get("files") or []
    return {
        "status_code": res["status_code"],
        "html_url": data.get("html_url"),
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits"),
        "files": files_out,
        "files_truncated": len(files_out) < len(all_files) or patch_budget <= 0,
    }
