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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset

# Kunci meta JSON Schema yang tidak boleh diterjemahkan jadi field.
# Server filesystem MCP memakai draft 2020-12 dan mengirim "$schema" (S3).
META_KEYS = frozenset({"$schema", "$id", "$comment", "title", "definitions", "$defs"})

# Tool yang tidak boleh dipanggil dari dalam sandbox: butuh interaksi,
# kredensial, atau kontrol alur yang sandbox tidak punya. Tool seperti itu
# tidak dibuang - dirutekan ke jalur `direct` (Fase 5): dideklarasikan apa
# adanya ke model, sehingga HITL/confirmation ADK berjalan normal.


class DirectPolicy(StrEnum):
    """Seberapa agresif tool dirutekan ke jalur `direct`.

    Catatan kesetiaan: blog Cloudflare **tidak membahas** tool destruktif,
    jadi default dibuat tidak mengubah perilaku Fase 0-4.
    """

    #: Hanya tool yang memang tidak bisa jalan di sandbox (konfirmasi,
    #: kredensial interaktif). Default.
    OFF = "off"
    #: Tambah: tool yang anotasi MCP-nya menyatakan destruktif / bukan
    #: read-only. Tool **tanpa** anotasi tetap di sandbox.
    ANNOTATIONS = "annotations"
    #: Setia pada spesifikasi MCP: `destructiveHint` default-nya **true**,
    #: jadi anotasi yang absen dianggap destruktif. Agresif - bisa
    #: mengosongkan sandbox pada server yang tidak menulis anotasi.
    ANNOTATIONS_STRICT = "annotations-strict"


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
class DirectTool:
    """Tool yang sengaja TIDAK diekspos ke sandbox.

    Bukan dibuang: `tool` disimpan supaya `CodeMode` bisa mendeklarasikannya
    langsung ke model sebagai tool ADK biasa. Dengan begitu rantai callback,
    `require_confirmation`, dan alur kredensial berjalan lewat jalur bawaan
    ADK - bukan lewat replikasi kami di `RpcBridge`.
    """

    server: str
    name: str
    reason: str
    tool: BaseTool | None = None

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"


#: Nama tipe bersama untuk envelope MCP. Dipancarkan SEKALI, lalu di-`extends`
#: oleh tiap interface Output. Tanpa ini, 58% deklarasi adalah boilerplate yang
#: sama diulang per tool (terukur: 24.926 dari 43.281 char pada 60 tool).
MCP_BASE = "McpResultBase"

SHARED_PREFIX = "#/shared/"

MCP_BASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "description": (
                "Blok konten MCP. Teksnya biasanya sama dengan "
                "structuredContent, hanya sudah dirender."
            ),
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["type"],
                "additionalProperties": True,
            },
        },
        "isError": {
            "description": (
                "Selalu false di sini - kegagalan tool dilempar sebagai "
                "exception, jadi tidak perlu diperiksa."
            ),
            "type": "boolean",
        },
    },
    "required": ["content"],
}


@dataclass
class Catalog:
    tools: list[ToolSpec] = field(default_factory=list)
    #: Tool yang dialihkan ke jalur `direct`, tidak terlihat dari sandbox.
    direct: list[DirectTool] = field(default_factory=list)
    #: Schema bernama yang dipancarkan sekali dan dirujuk lewat
    #: `$ref: "#/shared/<nama>"`. Codegen tidak perlu tahu isinya soal MCP.
    shared: dict[str, dict[str, Any]] = field(default_factory=dict)

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


#: Bentuk NYATA yang dikembalikan `McpTool.run_async` - envelope
#: `CallToolResult` utuh, bukan payload.
#:
#: Ini terukur, bukan dugaan: `outputSchema` MCP menjelaskan isi
#: `structuredContent`, tapi ADK meneruskan seluruh envelope. Kalau codegen
#: memakai `outputSchema` apa adanya, tipe yang dideklarasikan BERBEDA dari
#: nilai runtime - model akan menulis `r.content` mengharapkan string lalu
#: menerima array. Tipe yang menyesatkan lebih buruk daripada tanpa tipe.
def mcp_envelope(output_schema: dict[str, Any] | None) -> dict[str, Any]:
    structured: dict[str, Any] = output_schema or {
        "type": "object",
        "additionalProperties": True,
    }
    # Bentuk `allOf [$ref bersama, properti lokal]` - komposisi JSON Schema
    # standar. Codegen menerjemahkannya jadi `interface X extends Base { ... }`,
    # jadi bagian yang sama tidak diulang per tool.
    return {
        "allOf": [
            {"$ref": f"{SHARED_PREFIX}{MCP_BASE}"},
            {
                "type": "object",
                "properties": {
                    "structuredContent": {
                        **structured,
                        "description": (
                            "Payload terstruktur sesuai outputSchema tool."
                        ),
                    }
                },
            },
        ]
    }


def _extract_output(tool: BaseTool) -> tuple[dict[str, Any] | None, SchemaSource]:
    raw = getattr(tool, "_mcp_tool", None)
    if raw is None:
        # Tool non-MCP (FunctionTool dll) mengembalikan nilainya langsung,
        # tanpa envelope.
        return None, SchemaSource.NONE
    out = getattr(raw, "outputSchema", None)
    return (
        mcp_envelope(dict(out) if out else None),
        SchemaSource.MCP_PRIVATE if out else SchemaSource.GEMINI_PARAMS,
    )


def _annotations(tool: BaseTool) -> Any | None:
    """Anotasi MCP kalau ada. Tool non-MCP tidak punya."""
    raw = getattr(tool, "_mcp_tool", None)
    return getattr(raw, "annotations", None) if raw is not None else None


def _annotation_reason(tool: BaseTool, policy: DirectPolicy) -> str | None:
    """Alasan direct berdasarkan anotasi MCP.

    Dua jebakan spesifikasi MCP, keduanya ditangani sadar di sini:

    1. `destructiveHint` **default-nya true**, dan hanya bermakna saat
       `readOnlyHint == false`. Jadi "hint absen" tidak berarti "aman".
       `ANNOTATIONS` memilih menganggapnya aman (pragmatis - server resmi
       sering tidak menulis anotasi); `ANNOTATIONS_STRICT` setia pada spec.
    2. Spec menyatakan anotasi adalah **hint dari server yang mungkin tidak
       dipercaya**. Karena itu anotasi hanya boleh **memperketat** (menarik
       tool ke direct), tidak pernah melonggarkan: `readOnlyHint=true` tidak
       bisa membatalkan permintaan direct yang eksplisit - lihat urutan di
       `direct_reason`.
    """
    if policy is DirectPolicy.OFF:
        return None

    ann = _annotations(tool)
    if ann is None:
        if policy is DirectPolicy.ANNOTATIONS_STRICT:
            return (
                "tanpa anotasi MCP - spesifikasi menetapkan destructiveHint "
                "default true, jadi dianggap destruktif"
            )
        return None

    read_only = getattr(ann, "readOnlyHint", None)
    destructive = getattr(ann, "destructiveHint", None)

    if read_only is True:
        return None
    if destructive is True:
        return "destructiveHint=true pada anotasi MCP"
    if destructive is None and policy is DirectPolicy.ANNOTATIONS_STRICT:
        return (
            "destructiveHint tidak diisi dan readOnlyHint bukan true - "
            "default spesifikasi adalah destruktif"
        )
    if read_only is False and destructive is None:
        return "readOnlyHint=false pada anotasi MCP"
    return None


#: Nama tool (`tool` atau `server.tool`) atau predikat yang mengembalikan
#: alasan. Predikat dipakai kalau kebijakannya bergantung konteks.
DirectSelector = Sequence[str] | Callable[[BaseTool, str], str | None]


def _selector_reason(
    tool: BaseTool, server: str, selector: DirectSelector | None
) -> str | None:
    if selector is None:
        return None
    if callable(selector):
        return selector(tool, server)
    wanted = set(selector)
    if tool.name in wanted or f"{server}.{tool.name}" in wanted:
        return "diminta eksplisit lewat `direct=`"
    return None


def _rejection_reason(tool: BaseTool) -> str | None:
    # ADK menyimpannya sebagai `_require_confirmation` (privat). Nama publik
    # `require_confirmation` hanya ada di parameter __init__, jadi memeriksa
    # nama publik saja akan selalu melewatkan tool ini.
    if getattr(tool, "_require_confirmation", None) or getattr(
        tool, "require_confirmation", None
    ):
        return (
            "require_confirmation aktif - butuh persetujuan pengguna, tidak "
            "bisa diminta dari dalam sandbox"
        )

    # BaseAuthenticatedTool dengan auth_config: kalau kredensial belum ada,
    # `run_async` memanggil request_credential lalu MENGEMBALIKAN string
    # "Pending User Authorization." sebagai NILAI, bukan error. Di sandbox,
    # string itu akan dibaca sebagai hasil sukses - gagal senyap.
    if getattr(tool, "_credentials_manager", None) is not None:
        return (
            "butuh alur kredensial interaktif - run_async mengembalikan "
            "'Pending User Authorization.' sebagai nilai, yang di sandbox "
            "tampak seperti hasil sukses"
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
        output_schema=out_schema,
        input_source=in_src,
        output_source=out_src,
    )


def direct_reason(
    tool: BaseTool,
    server: str,
    *,
    policy: DirectPolicy = DirectPolicy.OFF,
    selector: DirectSelector | None = None,
) -> str | None:
    """Alasan tool harus lewat jalur `direct`, atau None kalau boleh di sandbox.

    Urutan disengaja: permintaan eksplisit menang, lalu batasan teknis yang
    membuat tool mustahil jalan di sandbox, baru anotasi server.
    """
    return (
        _selector_reason(tool, server, selector)
        or _rejection_reason(tool)
        or _annotation_reason(tool, policy)
    )


async def build_catalog(
    sources: dict[str, BaseToolset | Sequence[BaseTool]],
    *,
    direct: DirectSelector | None = None,
    direct_policy: DirectPolicy = DirectPolicy.OFF,
) -> Catalog:
    """Bangun katalog dari peta `nama_server -> toolset atau daftar tool`.

    Nama server dipakai sebagai namespace di API yang dibangkitkan.

    `direct` berisi nama tool atau predikat; tool tersebut tidak masuk
    sandbox dan dideklarasikan langsung ke model.
    """
    catalog = Catalog()
    for server, source in sources.items():
        tools = (
            await source.get_tools()
            if isinstance(source, BaseToolset)
            else list(source)
        )
        for tool in tools:
            reason = direct_reason(tool, server, policy=direct_policy, selector=direct)
            if reason:
                catalog.direct.append(DirectTool(server, tool.name, reason, tool))
                continue
            spec = spec_from_tool(tool, server)
            catalog.tools.append(spec)
            if spec.output_schema and "allOf" in spec.output_schema:
                catalog.shared.setdefault(MCP_BASE, MCP_BASE_SCHEMA)
    return catalog


def enable_json_schema_feature() -> None:
    """Aktifkan jalur publik untuk input schema.

    Tanpa ini, `parameters_json_schema` bernilai None dan Catalog jatuh ke
    jalur privat `_mcp_tool` (lihat S3).
    """
    from google.adk.features import FeatureName, override_feature_enabled

    override_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, True)
