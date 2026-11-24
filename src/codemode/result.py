"""ResultAdapter: satu-satunya pemilik "bentuk hasil tool di sandbox".

Kelas bug yang dieliminasi: tipe yang dideklarasikan ke model dan nilai yang
benar-benar dikirim ke sandbox diturunkan oleh jalur kode yang terpisah, jadi
bisa berbeda diam-diam (contoh nyata: bug `output_schema` snake_case).

Aturan:
  - `bridge` WAJIB mengirim nilai lewat `deliver()`.
  - `declared_schema()` adalah schema yang DIJANJIKAN ke model.
  - `conformance_errors()` membandingkan keduanya; test properti memakainya
    atas seluruh katalog. Mengubah `deliver()` tanpa mengubah schema (atau
    sebaliknya) membuat test itu gagal.

Saat ini `deliver()` identitas (envelope MCP utuh) - perilaku tidak berubah.
Membuka envelope (item 4) = ubah DUA hal di berkas ini, bukan tiga tempat.
"""

from __future__ import annotations

import copy
from typing import Any

from codemode.catalog import SHARED_PREFIX, Catalog, ToolSpec


def _inline_refs(node: Any, shared: dict[str, dict[str, Any]]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(SHARED_PREFIX):
            return _inline_refs(shared[ref[len(SHARED_PREFIX) :]], shared)
        return {k: _inline_refs(v, shared) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, shared) for v in node]
    return node


class ResultAdapter:
    def __init__(self, spec: ToolSpec, shared: dict[str, dict[str, Any]]):
        self._spec = spec
        self._shared = shared

    def deliver(self, raw: Any) -> Any:
        """Nilai yang diterima kode sandbox."""
        return raw

    def declared_schema(self) -> dict[str, Any] | None:
        """Schema hasil yang dijanjikan ke model; None = tak ada janji."""
        if self._spec.output_schema is None:
            return None
        return _inline_refs(copy.deepcopy(self._spec.output_schema), self._shared)

    def conformance_errors(self, delivered: Any) -> list[str]:
        """Kosong = nilai yang dikirim memenuhi tipe yang dijanjikan."""
        import jsonschema

        schema = self.declared_schema()
        if schema is None:
            return []
        validator = jsonschema.Draft202012Validator(schema)
        return [
            f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in validator.iter_errors(delivered)
        ]


def adapters_for(catalog: Catalog) -> dict[tuple[str, str], ResultAdapter]:
    return {(t.server, t.name): ResultAdapter(t, catalog.shared) for t in catalog.tools}
