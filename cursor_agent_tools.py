"""MCP tools that invoke Cursor's headless `agent` CLI (see https://cursor.com/docs/cli/headless)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

import memory_store
import tool_gating
from run_log import append_event, finish_run, redact_string, start_run

logger = logging.getLogger(__name__)

_DEFAULT_CURSOR_TIMEOUT = int(os.getenv("CURSOR_AGENT_TIMEOUT_SECONDS", "900"))
_MAX_AGENT_OUTPUT_CHARS = int(os.getenv("CURSOR_AGENT_MAX_OUTPUT_CHARS", "120000"))


def _workspace_roots() -> list[Path]:
    raw = (os.getenv("CURSOR_WORKSPACE_ROOTS") or "").strip()
    if not raw:
        return []
    parts = []
    for chunk in raw.replace(";", os.pathsep).split(os.pathsep):
        c = chunk.strip().strip('"')
        if c:
            parts.append(Path(c))
    return parts


def _is_under_allowed_root(resolved: Path, roots: list[Path]) -> bool:
    try:
        rp = resolved.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            rr = root.resolve()
        except OSError:
            continue
        try:
            rp.relative_to(rr)
            return True
        except ValueError:
            continue
    return False


def _find_agent_exe() -> str | None:
    explicit = (os.getenv("CURSOR_AGENT_PATH") or "").strip()
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        return explicit
    return shutil.which("agent")


def _resolve_workspace(workspace_path: str) -> tuple[Path | None, dict[str, Any] | None]:
    """Return (resolved_path, error_dict) on failure."""
    try:
        ws = Path(workspace_path).expanduser()
        ws_resolved = ws.resolve()
    except OSError as e:
        return None, {"error": f"invalid workspace_path: {e}"}
    if not ws_resolved.is_dir():
        return None, {"error": "workspace_path is not a directory", "path": str(ws_resolved)}
    roots = _workspace_roots()
    if not roots:
        return None, {
            "error": "CURSOR_WORKSPACE_ROOTS is not configured",
            "hint": "Set to a semicolon-separated list of absolute directories (Windows), e.g. C:\\\\Code\\\\myrepo",
        }
    if not _is_under_allowed_root(ws_resolved, roots):
        return None, {
            "error": "workspace_path is not under any directory in CURSOR_WORKSPACE_ROOTS",
            "resolved": str(ws_resolved),
            "allowed_roots": [str(r) for r in roots],
        }
    return ws_resolved, None


def register_cursor_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def approve_cursor_writes(
        workspace_path: str,
        always_allow_level_3_rule: bool = False,
    ) -> dict[str, Any]:
        """
        Persistently allow capability_level=3 (apply + --force) for this workspace on this machine.
        workspace_path must lie under CURSOR_WORKSPACE_ROOTS. Revoke via revoke_cursor_writes.
        If always_allow_level_3_rule=true, stores a durable rule so Level 3 stays allowed until revoke
        (clears both session flag and rule).
        """
        if (g := tool_gating.tool_disabled_error("approve_cursor_writes")) is not None:
            return g
        ws_resolved, err = _resolve_workspace(workspace_path)
        if err:
            return err
        assert ws_resolved is not None
        memory_store.set_cursor_write_allowed(str(ws_resolved), True)
        if always_allow_level_3_rule:
            memory_store.set_cursor_always_allow_level_3(str(ws_resolved), True)
        return {
            "ok": True,
            "workspace": str(ws_resolved),
            "always_allow_level_3_rule": always_allow_level_3_rule,
            "message": "Level 3 (apply changes) is now allowed for this workspace until revoked.",
        }

    @mcp.tool()
    async def revoke_cursor_writes(workspace_path: str) -> dict[str, Any]:
        """Remove persisted Level-3 permission and any always-allow rule for this workspace."""
        if (g := tool_gating.tool_disabled_error("revoke_cursor_writes")) is not None:
            return g
        ws_resolved, err = _resolve_workspace(workspace_path)
        if err:
            return err
        assert ws_resolved is not None
        memory_store.set_cursor_write_allowed(str(ws_resolved), False)
        return {"ok": True, "workspace": str(ws_resolved), "message": "Level 3 permission cleared."}

    @mcp.tool()
    async def cursor_agent(
        prompt: str,
        workspace_path: str,
        capability_level: Literal[1, 2, 3] = 2,
        apply_changes: bool = False,
        output_format: Literal["text", "json"] = "text",
        mode: Literal["agent", "ask", "plan"] = "agent",
        use_trust_flag: bool = True,
    ) -> dict[str, Any]:
        """
        Run Cursor Agent CLI (`agent --print`) against an allowlisted workspace.

        **Capability levels (Grok-oriented):**
        - **1 – Read/Analyze:** `--mode ask`, never applies file changes (`--force` off).
        - **2 – Propose (default):** `--mode plan`, shows intended changes without applying.
        - **3 – Apply:** `--mode agent` + `--force` only if this workspace was approved via
          `approve_cursor_writes` (or `always_allow_level_3_rule=true`) or **`cursor_rules.always_allow_level_3`** in memory.

        If `apply_changes=true`, that requests Level 3 (same as `capability_level=3`).
        The free `mode` parameter is ignored when derived mode comes from levels 1–3
        (levels always set CLI mode and force as in the table above).
        """
        if (g := tool_gating.tool_disabled_error("cursor_agent")) is not None:
            return g
        if not (os.getenv("CURSOR_API_KEY") or "").strip():
            return {
                "error": "CURSOR_API_KEY is not set",
                "hint": "https://cursor.com/docs/cli/reference/authentication",
            }

        exe = _find_agent_exe()
        if not exe:
            return {
                "error": "Cursor `agent` executable not found",
                "hint": "Install CLI: https://cursor.com/docs/cli/installation — or set CURSOR_AGENT_PATH",
            }

        if not prompt or not prompt.strip():
            return {"error": "prompt must be non-empty"}

        ws_resolved, err = _resolve_workspace(workspace_path)
        if err:
            return err
        assert ws_resolved is not None

        if output_format not in ("text", "json"):
            return {"error": "output_format must be text or json"}
        if mode not in ("agent", "ask", "plan"):
            return {"error": "mode must be agent, ask, or plan"}

        try:
            cl = int(capability_level)
        except (TypeError, ValueError):
            return {"error": "capability_level must be 1, 2, or 3"}
        if cl not in (1, 2, 3):
            return {"error": "capability_level must be 1, 2, or 3"}

        effective_level: int = 3 if apply_changes else cl

        wants_force = effective_level == 3
        if wants_force and not memory_store.is_cursor_write_allowed(str(ws_resolved)):
            return {
                "error": "capability_level_3_not_approved",
                "hint": "Call approve_cursor_writes (optionally always_allow_level_3_rule=true) for this workspace_path first, or use capability_level=2 (plan) / 1 (ask).",
                "workspace": str(ws_resolved),
            }

        if effective_level == 1:
            cli_mode = "ask"
            use_force = False
        elif effective_level == 2:
            cli_mode = "plan"
            use_force = False
        else:
            cli_mode = "agent"
            use_force = True

        run_id = start_run(
            "cursor_agent",
            {
                "workspace": str(ws_resolved),
                "capability_level": effective_level,
                "apply_changes": apply_changes,
                "legacy_mode_param": mode,
                "cli_mode": cli_mode,
                "use_force": use_force,
                "output_format": output_format,
                "prompt_chars": len(prompt.strip()),
            },
        )

        cmd: list[str] = [
            exe,
            "-p",
            "--workspace",
            str(ws_resolved),
        ]

        if output_format != "text":
            cmd.extend(["--output-format", output_format])

        if cli_mode == "ask":
            cmd.extend(["--mode", "ask"])
        elif cli_mode == "plan":
            cmd.extend(["--mode", "plan"])
        else:
            cmd.extend(["--mode", "agent"])

        if use_trust_flag:
            cmd.append("--trust")

        if use_force:
            cmd.append("--force")
            append_event(
                run_id,
                {
                    "kind": "cursor_apply",
                    "workspace": str(ws_resolved),
                    "note": "high_impact: agent --force",
                },
            )

        cmd.append(prompt.strip())

        append_event(
            run_id,
            {
                "kind": "cmd",
                "exe": exe,
                "argv_tail": redact_string(" ".join(cmd[1:-1])) + f" [prompt_len={len(prompt.strip())}]",
            },
        )

        timeout = max(30, min(_DEFAULT_CURSOR_TIMEOUT, 3600))

        env = os.environ.copy()
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        logger.info(
            "cursor_agent: cwd=%s timeout=%s level=%s cli_mode=%s force=%s",
            ws_resolved,
            timeout,
            effective_level,
            cli_mode,
            use_force,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ws_resolved),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                creationflags=creationflags,
            )
        except OSError as e:
            append_event(run_id, {"kind": "spawn_error", "message": str(e)[:500]})
            finish_run(run_id, "error", {"stage": "spawn"})
            return {"run_id": run_id, "error": "failed_to_spawn_agent", "message": str(e)}

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
        except asyncio.TimeoutError:
            proc.kill()
            append_event(run_id, {"kind": "timeout", "timeout_seconds": timeout})
            finish_run(run_id, "timeout", {})
            return {"run_id": run_id, "error": "timeout", "timeout_seconds": timeout}

        out = out_b.decode("utf-8", errors="replace") if out_b else ""
        err = err_b.decode("utf-8", errors="replace") if err_b else ""
        truncated = False
        combined = out
        if err:
            combined = f"{out}\n--- stderr ---\n{err}"
        if len(combined) > _MAX_AGENT_OUTPUT_CHARS:
            combined = combined[:_MAX_AGENT_OUTPUT_CHARS] + "\n... [truncated]"
            truncated = True

        append_event(
            run_id,
            {
                "kind": "process_done",
                "exit_code": proc.returncode,
                "output_chars": len(combined),
                "truncated": truncated,
            },
        )
        finish_run(
            run_id,
            "success" if proc.returncode == 0 else "failed",
            {"exit_code": proc.returncode},
        )

        return {
            "run_id": run_id,
            "exit_code": proc.returncode,
            "stdout_stderr": combined,
            "truncated": truncated,
            "command": " ".join(cmd[:-1]) + " [prompt]",
            "capability_level": effective_level,
            "cli_mode": cli_mode,
            "force": use_force,
        }
