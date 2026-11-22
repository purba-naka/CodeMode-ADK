"""Gerbang keluar Fase 5c - precheck type-check, MCP server sungguhan, tanpa LLM.

Yang diukur bukan "apakah deno check jalan", tapi dua angka yang menentukan
apakah fitur ini layak dinyalakan:

1. **Tingkat tangkapan** pada korpus kode rusak yang meniru kegagalan model
   nyata (nama tool salah, argumen wajib hilang, tipe tidak cocok, lupa
   `await`, properti hasil yang tidak ada).
2. **False positive = 0** pada korpus kode valid - termasuk skenario F3 yang
   sudah terbukti berjalan di server sungguhan. Satu saja false positive
   membuat fitur ini lebih merusak daripada berguna: ia akan menolak kode yang
   sebenarnya benar, dan model tidak punya cara membedakannya.

Plus biaya latensinya, supaya keputusan menyalakan/mematikan punya angka.
"""

from __future__ import annotations

import asyncio
import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from codemode.adk import CodeMode
from codemode.sandbox import Limits

ROOT = Path(__file__).parent.parent

# --- korpus RUSAK: tiap entri HARUS ditolak precheck
RUSAK: list[tuple[str, str]] = [
    (
        "nama tool salah",
        'const r = await tools.read_text_filee({ path: "/a.txt" });\nconsole.log(r);',
    ),
    (
        "argumen wajib hilang",
        "const r = await tools.read_text_file({});\nconsole.log(r);",
    ),
    (
        "tipe argumen salah",
        "const r = await tools.read_text_file({ path: 123 });\nconsole.log(r);",
    ),
    (
        "argumen tidak dikenal",
        'const r = await tools.read_text_file({ path: "/a.txt", limit: 5 });'
        "\nconsole.log(r);",
    ),
    (
        "lupa await lalu akses properti",
        'const r = tools.read_text_file({ path: "/a.txt" });'
        "\nconsole.log(r.structuredContent.content.length);",
    ),
    (
        "properti hasil tidak ada",
        'const r = await tools.get_file_info({ path: "/a.txt" });'
        "\nconsole.log(r.ukuranBerkas);",
    ),
    (
        "tipe kembalian disalahartikan",
        'const r = await tools.list_directory({ path: "." });'
        "\nconst n: number = r.structuredContent.content;\nconsole.log(n);",
    ),
]

# --- korpus VALID: tiap entri HARUS lolos
VALID: list[tuple[str, str]] = [
    (
        "satu call",
        'const r = await tools.read_text_file({ path: "/a.txt" });'
        "\nconsole.log(r.structuredContent?.content);",
    ),
    (
        "rantai + filter",
        """
const files = await tools.list_directory({ path: "." });
const names = (files.structuredContent?.content ?? "")
  .split("\\n")
  .filter((l: string) => l.includes(".txt"))
  .map((l: string) => l.replace("[FILE]", "").trim())
  .filter(Boolean);
let total = 0;
for (const n of names) {
  const f = await tools.read_text_file({ path: n });
  total += (f.structuredContent?.content ?? "").split("\\n").length;
}
console.log("total:", total);
""",
    ),
    (
        "try/catch + Promise.all",
        """
try {
  const hasil = await Promise.all([
    tools.get_file_info({ path: "/a.txt" }),
    tools.list_allowed_directories({}),
  ]);
  console.log(hasil.length);
} catch (e) {
  console.log((e as Error).name);
}
""",
    ),
    (
        "interface + generic + top-level await",
        """
interface Ringkas { nama: string; baris: number }
const kumpul: Ringkas[] = [];
function tambah<T extends { nama: string }>(x: T, baris: number): void {
  kumpul.push({ nama: x.nama, baris });
}
tambah({ nama: "a.txt" }, 2);
const r = await tools.read_text_file({ path: "/a.txt" });
console.log(kumpul.length, typeof r);
""",
    ),
    (
        "argumen opsional dipakai",
        'const r = await tools.read_text_file({ path: "/a.txt", head: 5 });'
        "\nconsole.log(r.content[0].text);",
    ),
]


async def main() -> int:
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    target = ROOT / "build" / "fixtures_f5c"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "a.txt").write_text("satu\ndua\n", encoding="utf-8")

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

    cm = CodeMode({"fs": toolset}, limits=Limits(timeout_s=60), precheck=True)
    failures: list[str] = []

    try:
        await cm.prepare()
        checker = cm._checker
        assert checker is not None, "precheck tidak aktif"

        # pemanasan: `deno check` pertama mengisi cache (~2,3 s), dan
        # memasukkannya ke statistik akan menyesatkan.
        await checker.check('console.log("pemanasan");')

        print("--- korpus RUSAK (harus DITOLAK)")
        lolos_padahal_rusak: list[str] = []
        ms_rusak: list[float] = []
        for nama, kode in RUSAK:
            r = await checker.check(kode)
            ms_rusak.append(r.duration_ms)
            baris = (r.errors.splitlines() or [""])[0][:70]
            tanda = "DITOLAK" if not r.ok else "LOLOS <-- MASALAH"
            print(f"  {nama:34} {tanda:18} {r.duration_ms:5.0f}ms {baris}")
            if r.ok:
                lolos_padahal_rusak.append(nama)

        print("\n--- korpus VALID (harus LOLOS)")
        ditolak_padahal_valid: list[str] = []
        ms_valid: list[float] = []
        for nama, kode in VALID:
            r = await checker.check(kode)
            ms_valid.append(r.duration_ms)
            tanda = "lolos" if r.ok else "DITOLAK <-- FALSE POSITIVE"
            print(f"  {nama:34} {tanda:18} {r.duration_ms:5.0f}ms")
            if not r.ok:
                ditolak_padahal_valid.append(nama)
                for b in r.errors.splitlines()[:4]:
                    print(f"      | {b[:100]}")

        tangkap = len(RUSAK) - len(lolos_padahal_rusak)
        print(f"\ntingkat tangkapan : {tangkap}/{len(RUSAK)}")
        print(f"false positive    : {len(ditolak_padahal_valid)}/{len(VALID)}")
        print(
            f"latensi           : valid median={statistics.median(ms_valid):.0f}ms, "
            f"rusak median={statistics.median(ms_rusak):.0f}ms"
        )

        # False positive adalah kegagalan mutlak: menolak kode benar lebih
        # buruk daripada tidak memeriksa sama sekali.
        if ditolak_padahal_valid:
            failures.append(f"false positive: {ditolak_padahal_valid}")
        # Tangkapan tidak harus 100% - type-check tidak bisa melihat kesalahan
        # logika - tapi di bawah ini fitur tidak membayar ongkosnya.
        if tangkap < len(RUSAK) - 1:
            failures.append(
                f"tangkapan terlalu rendah: {tangkap}/{len(RUSAK)}, "
                f"lolos: {lolos_padahal_rusak}"
            )

        # --- precheck harus benar-benar MENCEGAH eksekusi, bukan cuma melapor
        from google.adk.agents import LlmAgent
        from google.adk.agents.context import Context
        from google.adk.agents.invocation_context import InvocationContext
        from google.adk.sessions import InMemorySessionService

        terlihat: list[str] = []

        def audit(*, tool, args, tool_context):
            terlihat.append(tool.name)
            return None

        agent = LlmAgent(
            name="gate5c",
            model="gemini-2.0-flash",
            instruction=cm.instruction_provider(),
            tools=cm.tools,
            before_tool_callback=audit,
        )
        svc = InMemorySessionService()
        session = await svc.create_session(app_name="f5c", user_id="u1")
        ictx = InvocationContext(
            invocation_id="inv-1", agent=agent, session=session, session_service=svc
        )
        ctx = Context(ictx, function_call_id="fc-exec")

        # Rantai yang separuhnya sah: tanpa precheck, dua call pertama JALAN
        # lalu gagal di tengah. Dengan precheck, nol call.
        separuh = """
const a = await tools.list_directory({ path: "." });
const b = await tools.get_file_info({ path: "/a.txt" });
console.log(a.content.length, b.content.length);
await tools.read_text_file({ path: 999 });
"""
        out = await cm.tool.run_async(args={"code": separuh}, tool_context=ctx)
        print(f"\nrantai separuh sah: status={out['status']} calls={out['tool_calls']}")
        print(f"  callback lihat  : {terlihat or '(tidak ada)'}")
        print(f"  error ke model  : {(out.get('error') or '')[:150]}")
        if out["status"] != "type_error":
            failures.append(f"status bukan type_error: {out['status']}")
        if out["tool_calls"] != 0 or terlihat:
            failures.append(f"tool TERPANGGIL padahal gagal type-check: {terlihat}")

        rep = cm.report()
        print(f"  laporan         : {rep['precheck']}")
    finally:
        await cm.close()

    print()
    if failures:
        print("GERBANG FASE 5c: GAGAL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GERBANG FASE 5c: LOLOS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
