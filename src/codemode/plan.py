"""Plan: keputusan yang diambil SEKALI di prepare(), lalu dibekukan.

Kelas bug yang dieliminasi: dua jalur menghitung ulang keputusan yang sama
dan berbeda pendapat. Sebelum Plan, `flat` diputuskan di tiga tempat
(codegen, CodeMode.execute, manifest_from_catalog). Sekarang hilir hanya
MEMBACA Plan; tidak ada yang memutuskan ulang.
"""

from __future__ import annotations

from dataclasses import dataclass

from codemode.catalog import Catalog


@dataclass(frozen=True)
class Plan:
    language: str
    flat: bool
    precheck: bool
    #: server -> nama tool yang boleh dipanggil dari sandbox (urut katalog).
    code_tools: tuple[tuple[str, str], ...]
    #: (server, nama, alasan) tool yang dirutekan ke jalur direct.
    direct_tools: tuple[tuple[str, str, str], ...]

    def manifest(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for server, name in self.code_tools:
            out.setdefault(server, []).append(name)
        return out

    def explain(self) -> str:
        lines = [
            f"language={self.language} flat={self.flat} precheck={self.precheck}",
            f"code ({len(self.code_tools)}): "
            + ", ".join(f"{s}.{n}" for s, n in self.code_tools),
            f"direct ({len(self.direct_tools)}):",
        ]
        lines += [f"  {s}.{n}: {why}" for s, n, why in self.direct_tools]
        return "\n".join(lines)


def make_plan(
    catalog: Catalog,
    *,
    language: str,
    flat_override: bool | None,
    precheck: bool,
) -> Plan:
    flat = catalog.is_single_server if flat_override is None else flat_override
    return Plan(
        language=language,
        flat=flat,
        precheck=precheck,
        code_tools=tuple((t.server, t.name) for t in catalog.tools),
        direct_tools=tuple((d.server, d.name, d.reason) for d in catalog.direct),
    )
