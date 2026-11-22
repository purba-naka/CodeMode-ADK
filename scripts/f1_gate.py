"""Gerbang keluar Fase 1.

1. Bangun katalog dari server MCP filesystem sungguhan (14 tool).
2. Render deklarasi TypeScript.
3. `deno check` atas deklarasi  -> HARUS lolos.
4. `deno check` atas probe POSITIF -> HARUS lolos.
5. `deno check` atas probe NEGATIF -> HARUS GAGAL.

Langkah 5 yang membuat gerbang ini berarti: tanpa itu, deklarasi yang semuanya
`any` juga akan "lolos".
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from codemode.catalog import SchemaSource, build_catalog, enable_json_schema_feature
from codemode.codegen import typescript

ROOT = Path(__file__).parent.parent
OUT = ROOT / "build"
DENO = (
    shutil.which("deno.exe")
    or shutil.which("deno")
    or str(Path(os.environ.get("APPDATA", "")) / "npm/node_modules/deno/deno.exe")
)

PROBE_OK = """
/// <reference path="./tools.d.ts" />
// Probe POSITIF: pemakaian yang benar harus type-check.
const txt = await tools.read_text_file({ path: "/a.txt", head: 5 });
console.log(txt);

const listed = await tools.list_directory({ path: "/" });
console.log(listed);

// rantai: hasil satu call jadi input call berikutnya
const info = await tools.get_file_info({ path: "/a.txt" });
console.log(JSON.stringify(info).length);
"""

PROBE_BAD = """
/// <reference path="./tools.d.ts" />
// Probe NEGATIF: tiap baris di bawah HARUS memicu error type-check.
await tools.read_text_file({ path: 123 });            // path bukan string
await tools.read_text_file({});                        // path wajib, hilang
await tools.tool_yang_tidak_ada({ path: "/a" });        // tool tidak ada
"""


def deno_check(path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [DENO, "check", "--no-lock", str(path)],
        capture_output=True,
        text=True,
        cwd=OUT,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


async def main() -> int:
    enable_json_schema_feature()

    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    target = ROOT / "build" / "fixtures_f1"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "a.txt").write_text("satu\ndua\ntiga\n", encoding="utf-8")

    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=npx,
                args=["-y", "@modelcontextprotocol/server-filesystem", str(target)],
            ),
            timeout=60,
        )
    )

    OUT.mkdir(exist_ok=True)
    failures: list[str] = []

    try:
        catalog = await build_catalog({"fs": toolset})
    finally:
        await toolset.close()

    # --- laporan katalog ---
    print(f"tool          : {len(catalog.tools)}")
    print(f"jalur direct  : {len(catalog.direct)}")
    print(f"server        : {catalog.servers} (single={catalog.is_single_server})")
    src_in = {s.input_source for s in catalog.tools}
    src_out = {s.output_source for s in catalog.tools}
    print(f"sumber input  : {[s.value for s in src_in]}")
    print(f"sumber output : {[s.value for s in src_out]}")

    if src_in != {SchemaSource.DECLARATION}:
        failures.append(f"input schema tidak semua dari jalur publik: {src_in}")
    n_out = sum(1 for s in catalog.tools if s.output_schema)
    print(f"punya outputSchema: {n_out}/{len(catalog.tools)}")

    # --- render ---
    decl = typescript.render(catalog)  # flat otomatis: satu server
    (OUT / "tools.d.ts").write_text(decl, encoding="utf-8")
    (OUT / "probe_ok.ts").write_text(PROBE_OK, encoding="utf-8")
    (OUT / "probe_bad.ts").write_text(PROBE_BAD, encoding="utf-8")

    n_lines = decl.count("\n")
    print(
        f"deklarasi     : {n_lines} baris, {len(decl)} char "
        f"(~{len(decl) // 4} token kasar)"
    )

    # --- gerbang ---
    print("\n--- deno check")
    ok, msg = deno_check(OUT / "tools.d.ts")
    print(f"  deklarasi   : {'LOLOS' if ok else 'GAGAL'}")
    if not ok:
        failures.append("deklarasi gagal type-check")
        print(msg[:1500])

    ok, msg = deno_check(OUT / "probe_ok.ts")
    print(f"  probe OK    : {'LOLOS' if ok else 'GAGAL'}")
    if not ok:
        failures.append("probe positif gagal type-check")
        print(msg[:1500])

    ok, msg = deno_check(OUT / "probe_bad.ts")
    n_err = msg.count("TS2") + msg.count("error:")
    print(
        f"  probe BAD   : {'GAGAL (benar)' if not ok else 'LOLOS (SALAH!)'}"
        f"  ~{n_err} error terdeteksi"
    )
    if ok:
        failures.append(
            "probe negatif LOLOS - tipe tidak mengikat, kemungkinan semuanya any"
        )
    else:
        for line in msg.splitlines():
            if "TS" in line and "error" in line.lower():
                print(f"    {line.strip()[:110]}")

    print()
    if failures:
        print("GERBANG FASE 1: GAGAL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GERBANG FASE 1: LOLOS")
    print(f"  artefak di {OUT}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GOOGLE_API_KEY", "dummy")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
