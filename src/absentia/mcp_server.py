"""m3 — stdio MCP server exposing absence_check / absence_verify.

Implemented against the MCP stdio transport directly (newline-delimited
JSON-RPC 2.0), so the server has **no** third-party dependency and cannot break
the CLI when an SDK is absent. Client configuration is the usual

    {"mcpServers": {"absentia": {"command": "absentia", "args": ["mcp",
      "--archive", "/path/to/archive"]}}}

stdout carries protocol frames only; every diagnostic goes to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, IO

from . import __version__
from . import attest

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "absentia"

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string", "description": "要查证的命题（中文）。"},
        "archive": {"type": "string", "description": "档案目录；省略时使用 server 的 --archive。"},
        "tau": {"type": "number", "description": "判定阈值 τ，默认 0.62。"},
        "endpoint": {
            "type": "string",
            "description": "本机 OpenAI 兼容端点，如 http://127.0.0.1:11434/v1。",
        },
        "embed_model": {"type": "string", "description": "嵌入模型名，默认 bge-m3。"},
        "model": {"type": "string", "description": "裁决模型名（辅助，不参与判定）。"},
        "out_dir": {"type": "string", "description": "凭证输出目录，默认 .absentia。"},
    },
    "required": ["claim"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "attestation": {"type": "string", "description": "attestation.json 路径。"},
        "archive": {"type": "string", "description": "覆盖档案目录。"},
        "tau": {"type": "number", "description": "覆盖阈值 τ。"},
        "endpoint": {"type": "string", "description": "本机 OpenAI 兼容端点。"},
    },
    "required": ["attestation"],
}

TOOLS = (
    {
        "name": "absence_check",
        "description": (
            "对本地档案执行多口径 N×M 混合打分，返回带覆盖度的缺席凭证"
            "（PRESENT / ABSENT_BOUNDED / INCONCLUSIVE）。"
        ),
        "inputSchema": CHECK_SCHEMA,
    },
    {
        "name": "absence_verify",
        "description": "重算一份 attestation.json 并报告是否与声明一致。",
        "inputSchema": VERIFY_SCHEMA,
    },
)


class ToolError(Exception):
    """A tool-level failure reported to the client as isError."""


def _text(payload: Any) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": False,
    }


def _failure(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_absence_check(arguments: dict, default_archive: Path | None) -> dict:
    from .render import headline, summary_line

    claim = arguments.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        raise ToolError("claim 必须是非空字符串")
    archive_value = arguments.get("archive") or (str(default_archive) if default_archive else None)
    if not archive_value:
        raise ToolError("缺少 archive：在工具参数或 server 的 --archive 中给出档案目录")

    request = attest.CheckRequest.from_env(
        claim=claim,
        archive=Path(archive_value),
        out_dir=Path(arguments.get("out_dir") or ".absentia"),
        tau=arguments.get("tau"),
        endpoint=arguments.get("endpoint"),
        embed_model=arguments.get("embed_model"),
        model=arguments.get("model"),
    )
    envelope = attest.run_check(request)
    core = envelope["core"]
    return {
        "verdict": core["verdict"]["label"],
        "summary": summary_line(core),
        "headline": headline(core),
        "core_sha256": envelope["core_sha256"],
        "core": core,
        "attestation_path": str(request.out_dir / "attestation.json"),
        "ledger_path": str(request.out_dir / "ledger.md"),
    }


def call_absence_verify(arguments: dict, default_archive: Path | None) -> dict:
    path = arguments.get("attestation")
    if not isinstance(path, str) or not path:
        raise ToolError("attestation 必须是 attestation.json 的路径")
    archive_value = arguments.get("archive") or (
        str(default_archive) if default_archive else None
    )
    report = attest.run_verify(
        Path(path),
        archive=Path(archive_value) if archive_value else None,
        tau=arguments.get("tau"),
        endpoint=arguments.get("endpoint"),
    )
    report["summary"] = (
        "AGREES" if report["agreement"] else "DISAGREES"
    ) + f" 判定 {report['claimed_verdict']} → 复算 {report['recomputed_verdict']}"
    return report


def dispatch_tool(name: str, arguments: dict, default_archive: Path | None) -> dict:
    if name == "absence_check":
        return call_absence_check(arguments, default_archive)
    if name == "absence_verify":
        return call_absence_verify(arguments, default_archive)
    raise ToolError(f"未知工具：{name}")


def handle(message: dict, default_archive: Path | None = None) -> dict | None:
    """Return a JSON-RPC response, or None for notifications."""
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:  # notifications/initialized, cancelled, …
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def error(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return ok(
            {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": list(TOOLS)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return error(-32602, "tools/call 需要 name")
        try:
            result = dispatch_tool(name, arguments, default_archive)
        except ToolError as exc:
            return ok(_failure(str(exc)))
        except FileNotFoundError as exc:
            return ok(_failure(str(exc)))
        except Exception as exc:  # report, never kill the session
            print(f"absentia mcp: tool {name} failed: {exc!r}", file=sys.stderr)
            return ok(_failure(f"{type(exc).__name__}: {exc}"))
        return ok(_text(result))
    return error(-32601, f"未知方法：{method}")


def serve(
    default_archive: Path | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Blocking stdio loop; one JSON-RPC message per line."""
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    print(f"absentia mcp {__version__} ready on stdio", file=sys.stderr)
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc.msg}"},
            }
        else:
            if not isinstance(message, dict):
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "invalid request"},
                }
            else:
                response = handle(message, default_archive)
        if response is None:
            continue
        writer.write(json.dumps(response, ensure_ascii=False) + "\n")
        writer.flush()
