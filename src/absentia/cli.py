"""Command line surface: index / check / verify / mcp.

All four commands are thin: ingestion lives in ``ingest``, scoring in ``scan``,
the decision and its digest in ``attest``. This module only resolves arguments
(flag → environment → default), prints, and sets an exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import __version__
from . import attest, ingest
from .render import headline, summary_line

DEFAULT_OUT_DIR = Path(".absentia")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="本地档案查证：为「没有」出具带覆盖度的缺席凭证。",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"absentia {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="打印版本并退出。"
    ),
) -> None:
    """absentia — coverage-bounded absence attestations for local archives."""


@app.command()
def index(
    archive: Path = typer.Argument(..., exists=True, file_okay=False, help="档案目录。"),
    out: Path = typer.Option(DEFAULT_OUT_DIR, "--out", help="台账与索引输出目录。"),
) -> None:
    """解析 → 分页 → 切块，写带 sha256 的清单与解析失败台账。"""
    result = ingest.run_index(archive, out)
    totals = result.totals
    typer.echo(
        f"{totals['docs']} 文档 / {totals['pages']} 页（已解析 {totals['parsed_pages']}）/ "
        f"{totals['chunks']} 段 / {totals['excluded_docs']} 个文件未解析"
    )
    for entry in result.excluded:
        typer.echo(f"  未解析  {entry.path}  [{entry.reason}]  页数 {entry.pages}")
    typer.echo(f"清单  {out / ingest.MANIFEST_NAME}")
    typer.echo(f"索引  {out / ingest.INDEX_NAME}")
    typer.echo(f"manifest_sha256  {result.manifest['manifest_sha256']}")


@app.command()
def check(
    claim: str = typer.Argument(..., help="要查证的命题，例如「2009-2025 年有没有批准过摄像头采购」。"),
    archive: Path = typer.Option(..., "--archive", exists=True, file_okay=False, help="档案目录。"),
    out: Path = typer.Option(DEFAULT_OUT_DIR, "--out", help="凭证与台账输出目录。"),
    tau: float = typer.Option(attest.DEFAULT_TAU, "--tau", help="判定阈值 τ（0–1）。"),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="本机 OpenAI 兼容端点，如 http://127.0.0.1:11434/v1。"
    ),
    embed_model: str = typer.Option("bge-m3", "--embed-model", help="嵌入模型名。"),
    model: str | None = typer.Option(None, "--model", help="裁决用本机模型名（如 qwen3.8:27b-q8_0）。"),
    adjudicate: bool = typer.Option(False, "--adjudicate", help="用本机模型抽样复核（辅助，不参与判定）。"),
    as_json: bool = typer.Option(False, "--json", help="打印完整凭证 JSON。"),
) -> None:
    """多口径 N×M 混合打分并出具凭证（attestation.json + ledger.md）。"""
    request = attest.CheckRequest.from_env(
        claim,
        archive,
        out,
        tau=tau,
        endpoint=endpoint,
        embed_model=embed_model,
        model=model,
        adjudicate=adjudicate,
    )
    try:
        envelope = attest.run_check(request)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    core = envelope["core"]
    if as_json:
        typer.echo(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
        return

    typer.echo(headline(core))
    typer.echo(summary_line(core))
    if core["bound"]["lexical_only"]:
        error = core["bound"].get("embedding_error")
        if error:
            typer.echo(
                f"注意：嵌入端点不可用（{error}），本次只跑了词法通道，因此不出具 "
                "ABSENT_BOUNDED。端点恢复后重跑即可复算语义通道。",
                err=True,
            )
        else:
            typer.echo(
                "注意：未配置嵌入端点，本次只跑了词法通道，因此不出具 ABSENT_BOUNDED。"
                "配置 --endpoint 后可复算语义通道。",
                err=True,
            )
    typer.echo(f"凭证  {out / 'attestation.json'}")
    typer.echo(f"台账  {out / 'ledger.md'}")
    typer.echo(f"core_sha256  {envelope['core_sha256']}")


@app.command()
def verify(
    attestation: Path = typer.Argument(..., exists=True, dir_okay=False, help="attestation.json。"),
    archive: Path | None = typer.Option(
        None, "--archive", exists=True, file_okay=False, help="覆盖档案路径。"
    ),
    tau: float | None = typer.Option(None, "--tau", help="覆盖阈值 τ。"),
    endpoint: str | None = typer.Option(None, "--endpoint", help="本机 OpenAI 兼容端点。"),
    embed_model: str | None = typer.Option(None, "--embed-model", help="覆盖嵌入模型名。"),
    as_json: bool = typer.Option(False, "--json", help="打印复算报告 JSON。"),
) -> None:
    """重算一份凭证并报告是否一致。"""
    try:
        report = attest.run_verify(
            attestation,
            archive=archive,
            tau=tau,
            endpoint=endpoint,
            embed_model=embed_model,
        )
    except attest.AttestationError as exc:
        typer.echo(f"无法复算凭证：{exc}", err=True)
        raise typer.Exit(code=2)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        state = report.get("status") or ("AGREES" if report["agreement"] else "DISAGREES")
        typer.echo(
            f"{state}  判定 {report['claimed_verdict']} → 复算 {report['recomputed_verdict']}"
        )
        if report["recomputed_core_sha256"]:
            typer.echo(f"claimed core_sha256     {report['claimed_core_sha256']}")
            typer.echo(f"recomputed core_sha256  {report['recomputed_core_sha256']}")
        for difference in report["differences"]:
            typer.echo(f"  差异  {difference}")
        if report.get("note"):
            typer.echo(f"提示  {report['note']}")

    if not report["agreement"]:
        raise typer.Exit(code=4 if report.get("status") == "INCONCLUSIVE" else 3)


@app.command()
def mcp(
    archive: Path | None = typer.Option(
        None, "--archive", exists=True, file_okay=False, help="默认档案目录。"
    ),
) -> None:
    """起 stdio MCP server，暴露 absence_check / absence_verify。"""
    from . import mcp_server

    mcp_server.serve(default_archive=archive)


if __name__ == "__main__":  # pragma: no cover
    app()
