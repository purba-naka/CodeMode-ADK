"""Catalog - BaseTool ADK apa pun -> katalog netral.

Lapisan ini TIDAK tahu soal bahasa target dan TIDAK tahu soal MCP.
`McpTool` hanya kasus yang schema-nya lebih kaya (punya outputSchema).

Jalur pengambilan schema (lihat docs/spike-results.md S3):
  input  : _get_declaration().parameters_json_schema  <- jalur publik, identik
           dengan _mcp_tool.inputSchema
  output : _mcp_tool.outputSchema                     <- hanya jalur privat;
           kalau hilang, Codegen turun ke index signature
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset

# Kunci meta JSON Schema yang tidak boleh diterjemahkan jadi field.
# Server filesystem MCP memakai draft 2020-12 dan mengirim "$schema" (S3).
META_KEYS = frozenset({"$schema", "$id", "$comment", "title", "definitions", "$defs"})

# Tool yang tidak boleh dipanggil dari dalam sandbox: butuh interaksi,
# kredensial, atau kontrol alur yang sandbox tidak punya.
_BLOCKED_ATTRS = ("require_confirmation",)


class SchemaSource(StrEnum):
    """Dari mana schema diambil - untuk diagnostik dan pelacakan risiko."""

    DECLARATION = "declaration"  # jalur publik _get_declaration()
    MCP_PRIVATE = "mcp_private"  # jalur privat _mcp_tool
    GEMINI_PARAMS = "gemini_params"  # fallback lossy
    NONE = "none"


@dataclass(frozen=True)
class ToolSpec:
    """Satu tool, netral bahasa."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    input_source: SchemaSource
    output_source: SchemaSource

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


@dataclass(frozen=True)
class RejectedTool:
    """Tool yang sengaja tidak diekspos ke sandbox, beserta alasannya."""

    server: str
    name: str
    reason: str


@dataclass
class Catalog:
    tools: list[ToolSpec] = field(default_factory=list)
    rejected: list[RejectedTool] = field(default_factory=list)

    @property
    def servers(self) -> list[str]:
        seen: dict[str, None] = {}
        for t in self.tools:
            seen.setdefault(t.server, None)
        return list(seen)

    @property
    def is_single_server(self) -> bool:
        return len(self.servers) <= 1

    def by_server(self) -> dict[str, list[ToolSpec]]:
        out: dict[str, list[ToolSpec]] = {}
        for t in self.tools:
            out.setdefault(t.server, []).append(t)
        return out


def strip_meta(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Buang kunci meta di level atas. Tidak rekursif: `properties` boleh
    berisi field bernama `title`, dan itu field sungguhan."""
    if not schema:
        return {}
    return {k: v for k, v in schema.items() if k not in META_KEYS}


def _defs_of(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Ambil kamus definisi untuk resolusi $ref lokal."""
    if not schema:
        return {}
    return {**schema.get("definitions", {}), **schema.get("$defs", {})}


def _extract_input(tool: BaseTool) -> tuple[dict[str, Any], SchemaSource]:
    # 1. jalur publik - butuh FeatureName.JSON_SCHEMA_FOR_FUNC_DECL
    try:
        decl = tool._get_declaration()
    except Exception:
        decl = None

    if decl is not None:
        pjs = getattr(decl, "parameters_json_schema", None)
        if pjs:
            return dict(pjs), SchemaSource.DECLARATION

    # 2. jalur privat MCP
    raw = getattr(tool, "_mcp_tool", None)
    if raw is not None and getattr(raw, "inputSchema", None):
        return dict(raw.inputSchema), SchemaSource.MCP_PRIVATE

    # 3. fallback lossy: bentuk Gemini
    if decl is not None and getattr(decl, "parameters", None) is not None:
        dumped = decl.parameters.model_dump(exclude_none=True, by_alias=True)
        return dumped, SchemaSource.GEMINI_PARAMS

    return {}, SchemaSource.NONE


def _extract_output(tool: BaseTool) -> tuple[dict[str, Any] | None, SchemaSource]:
    raw = getattr(tool, "_mcp_tool", None)
    if raw is not None and getattr(raw, "outputSchema", None):
        return dict(raw.outputSchema), SchemaSource.MCP_PRIVATE
    return None, SchemaSource.NONE


def _rejection_reason(tool: BaseTool) -> str | None:
    for attr in _BLOCKED_ATTRS:
        val = getattr(tool, attr, None)
        if val:  # True atau callable
            return (
                f"{attr} aktif - butuh interaksi pengguna, tidak bisa dipanggil "
                "dari sandbox"
            )
    return None


def spec_from_tool(tool: BaseTool, server: str) -> ToolSpec:
    in_schema, in_src = _extract_input(tool)
    out_schema, out_src = _extract_output(tool)
    return ToolSpec(
        server=server,
        name=tool.name,
        description=(tool.description or "").strip(),
        input_schema=strip_meta(in_schema),
        output_schema=strip_meta(out_schema) if out_schema else None,
        input_source=in_src,
        output_source=out_src,
    )


async def build_catalog(
    sources: dict[str, BaseToolset | Sequence[BaseTool]],
) -> Catalog:
    """Bangun katalog dari peta `nama_server -> toolset atau daftar tool`.

    Nama server dipakai sebagai namespace di API yang dibangkitkan.
    """
    catalog = Catalog()
    for server, source in sources.items():
        tools = (
            await source.get_tools()
            if isinstance(source, BaseToolset)
            else list(source)
        )
        for tool in tools:
            reason = _rejection_reason(tool)
            if reason:
                catalog.rejected.append(RejectedTool(server, tool.name, reason))
                continue
            catalog.tools.append(spec_from_tool(tool, server))
    return catalog


def enable_json_schema_feature() -> None:
    """Aktifkan jalur publik untuk input schema.

    Tanpa ini, `parameters_json_schema` bernilai None dan Catalog jatuh ke
    jalur privat `_mcp_tool` (lihat S3).
    """
    from google.adk.features import FeatureName, override_feature_enabled

    override_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, True)
