"""Codegen TypeScript - katalog -> deklarasi API yang dibaca model.

Lapisan ini TIDAK tahu soal ADK.

Bentuknya meniru blog Cloudflare: dua interface per tool plus satu objek
deklarasi, satu argumen bernama `input`, mengembalikan `Promise<Output>`.

    interface SearchAgentsCodeInput  { query: string; page?: number }
    interface SearchAgentsCodeOutput { [key: string]: any }
    declare const tools: {
      /** doc comment dari description */
      search_agents_code: (input: SearchAgentsCodeInput)
        => Promise<SearchAgentsCodeOutput>;
    };
"""

from __future__ import annotations

import keyword
import re
from typing import Any

from codemode.catalog import Catalog, ToolSpec, _defs_of

UNKNOWN = "unknown"
LOOSE_OBJECT = "{ [key: string]: any }"
_MAX_DEPTH = 12
_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_WRAP = 72


def pascal(name: str) -> str:
    """`fetch_agents_documentation` -> `FetchAgentsDocumentation`."""
    parts = re.split(r"[^A-Za-z0-9]+", name)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _prop_key(name: str) -> str:
    """Kunci properti; dikutip kalau bukan identifier TS yang sah."""
    if _IDENT.match(name) and not keyword.iskeyword(name):
        return name
    return f'"{name}"'


def escape_doc(text: str) -> str:
    """Netralkan `*/` di dalam teks description.

    Penting, bukan kosmetik: deskripsi tool datang dari server MCP. Server
    filesystem resmi saja sudah mengandung `'**/*.ext'`, yang menutup blok
    JSDoc lebih awal dan merusak seluruh deklarasi. Server yang bermusuhan bisa
    memakai celah yang sama untuk menyuntikkan deklarasi TS buatan sendiri ke
    dalam konteks model.
    """
    return (text or "").replace("*/", "*\\/")


def _doc(text: str, indent: str) -> list[str]:
    """JSDoc multi-baris. Dipakai untuk description dari schema."""
    text = " ".join(escape_doc(text).split())
    if not text:
        return []
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > _WRAP:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    out = [f"{indent}/**"]
    out += [f"{indent} * {ln}" for ln in lines]
    out.append(f"{indent} */")
    return out


def _lit(value: Any) -> str:
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return UNKNOWN


_PRIMITIVES = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "null": "null",
}


def ts_type(
    schema: Any,
    *,
    indent: str = "",
    defs: dict[str, Any] | None = None,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> str:
    """JSON Schema -> tipe TypeScript.

    Subset yang didukung: object, array, primitif, enum, const, union lewat
    `type` berupa daftar, `anyOf`/`oneOf`, dan `$ref` lokal.
    Di luar itu -> `unknown`, dengan doc comment yang menjelaskan.
    """
    defs = defs or {}
    if depth > _MAX_DEPTH or not isinstance(schema, dict) or not schema:
        return UNKNOWN

    # $ref lokal, dengan penjaga siklus
    ref = schema.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in seen:
            return UNKNOWN  # siklus
        target = defs.get(key)
        if isinstance(target, dict):
            return ts_type(
                target,
                indent=indent,
                defs=defs,
                depth=depth + 1,
                seen=seen | {key},
            )
        return UNKNOWN

    if "const" in schema:
        return _lit(schema["const"])

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_lit(v) for v in enum)

    for kw in ("anyOf", "oneOf"):
        variants = schema.get(kw)
        if isinstance(variants, list) and variants:
            rendered = [
                ts_type(v, indent=indent, defs=defs, depth=depth + 1, seen=seen)
                for v in variants
            ]
            uniq = list(dict.fromkeys(rendered))
            return uniq[0] if len(uniq) == 1 else " | ".join(uniq)

    t = schema.get("type")
    if isinstance(t, list):
        rendered = [
            ts_type(
                {**schema, "type": one},
                indent=indent,
                defs=defs,
                depth=depth + 1,
                seen=seen,
            )
            for one in t
        ]
        return " | ".join(dict.fromkeys(rendered))

    if t == "array":
        items = schema.get("items")
        if isinstance(items, list):  # tuple form
            inner = ", ".join(
                ts_type(i, indent=indent, defs=defs, depth=depth + 1, seen=seen)
                for i in items
            )
            return f"[{inner}]"
        inner = ts_type(items, indent=indent, defs=defs, depth=depth + 1, seen=seen)
        return f"{inner}[]" if _IDENT.match(inner) else f"({inner})[]"

    if t == "object" or "properties" in schema:
        return _object_type(schema, indent=indent, defs=defs, depth=depth, seen=seen)

    if isinstance(t, str) and t in _PRIMITIVES:
        return _PRIMITIVES[t]

    return UNKNOWN


def _object_type(
    schema: dict[str, Any],
    *,
    indent: str,
    defs: dict[str, Any],
    depth: int,
    seen: frozenset[str],
) -> str:
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    addl = schema.get("additionalProperties")

    if not props:
        if addl is True or addl is None:
            return LOOSE_OBJECT
        if isinstance(addl, dict):
            inner = ts_type(addl, indent=indent, defs=defs, depth=depth + 1, seen=seen)
            return f"{{ [key: string]: {inner} }}"
        return "Record<string, never>"

    inner_indent = indent + "  "
    lines: list[str] = ["{"]
    for key, sub in props.items():
        if isinstance(sub, dict) and sub.get("description"):
            lines += _doc(sub["description"], inner_indent)
        rendered = ts_type(
            sub, indent=inner_indent, defs=defs, depth=depth + 1, seen=seen
        )
        opt = "" if key in required else "?"
        lines.append(f"{inner_indent}{_prop_key(key)}{opt}: {rendered};")
    if isinstance(addl, dict):
        extra = ts_type(
            addl, indent=inner_indent, defs=defs, depth=depth + 1, seen=seen
        )
        lines.append(f"{inner_indent}[key: string]: {extra};")
    elif addl is True:
        lines.append(f"{inner_indent}[key: string]: any;")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _iface_base(spec: ToolSpec, *, prefix_server: bool) -> str:
    head = pascal(spec.server) if prefix_server else ""
    return f"{head}{pascal(spec.name)}"


def _render_interfaces(spec: ToolSpec, *, prefix_server: bool) -> list[str]:
    base = _iface_base(spec, prefix_server=prefix_server)
    out: list[str] = []

    in_defs = _defs_of(spec.input_schema)
    in_body = ts_type(spec.input_schema, defs=in_defs) if spec.input_schema else ""
    if not in_body or not in_body.startswith("{"):
        in_body = LOOSE_OBJECT
    out.append(f"interface {base}Input {in_body}")

    if spec.output_schema:
        out_defs = _defs_of(spec.output_schema)
        out_body = ts_type(spec.output_schema, defs=out_defs)
        if not out_body.startswith("{"):
            out_body = f"{{ [key: string]: any }} /* {out_body} */"
    else:
        out_body = LOOSE_OBJECT
    out.append(f"interface {base}Output {out_body}")
    return out


def _render_member(spec: ToolSpec, *, prefix_server: bool, indent: str) -> list[str]:
    base = _iface_base(spec, prefix_server=prefix_server)
    lines = _doc(spec.description, indent)
    lines.append(
        f"{indent}{_prop_key(spec.name)}: (input: {base}Input) "
        f"=> Promise<{base}Output>;"
    )
    return lines


def render(catalog: Catalog, *, flat: bool | None = None) -> str:
    """Render deklarasi TS untuk seluruh katalog.

    `flat=None` -> otomatis: flat kalau cuma satu server (sesuai blog, yang
    memakai `declare const codemode` tanpa nesting). Dengan >1 server, nesting
    `tools.<server>.<tool>` dipakai untuk menghindari tabrakan nama.
    """
    if flat is None:
        flat = catalog.is_single_server

    prefix_server = not flat
    parts: list[str] = [
        "// Dibangkitkan otomatis dari schema MCP. Jangan diedit.",
        "// Satu-satunya cara kode ini menyentuh dunia luar adalah lewat `tools`.",
        "",
    ]

    for spec in catalog.tools:
        parts += _render_interfaces(spec, prefix_server=prefix_server)
    parts.append("")

    parts.append("declare const tools: {")
    if flat:
        for spec in catalog.tools:
            parts += _render_member(spec, prefix_server=False, indent="  ")
    else:
        for server, specs in catalog.by_server().items():
            parts.append(f"  {_prop_key(server)}: {{")
            for spec in specs:
                parts += _render_member(spec, prefix_server=True, indent="    ")
            parts.append("  };")
    parts.append("};")
    parts.append("")
    return "\n".join(parts)
