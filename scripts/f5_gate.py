"""Gerbang keluar Fase 5a - jalur `direct`, MCP server sungguhan, tanpa LLM.

Yang diuji: apakah tool destruktif benar-benar TIDAK bisa disentuh dari dalam
sandbox, sementara tetap tersedia bagi model sebagai tool biasa.

Lima pemeriksaan, semuanya bisa gagal:

1. Anotasi server memilah tool seperti yang diharapkan (3 destruktif dari 14).
2. Tool direct tidak muncul di deklarasi TypeScript.
3. Tool direct tetap dideklarasikan ke model lewat `direct_toolset`.
4. Jalur normal: memanggilnya dari kode gagal di sandbox.
5. Pemalsuan protokol lewat `Deno.stdout` mentah ditolak host, dan tool
   direct tidak pernah masuk rantai callback.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService

from codemode.adk import CodeMode
from codemode.catalog import DirectPolicy
from codemode.sandbox import Limits

ROOT = Path(__file__).parent.parent

DESTRUKTIF = {"write_file", "edit_file", "move_file"}

# Pemalsuan protokol: `console.log` dibajak runtime, `Deno.stdout.writeSync`
# tidak. Ini satu-satunya cara mencapai RpcBridge dengan nama tool direct.
SUNTIKAN = """
const enc = new TextEncoder();
Deno.stdout.writeSync(enc.encode(JSON.stringify({
  t: "call", id: 1, server: "fs", tool: "write_file",
  args: { path: "diretas.txt", content: "seharusnya tidak pernah ada" },
}) + "\\n"));
await new Promise((r) => setTimeout(r, 800));
console.log("sesudah suntikan");
"""


async def main() -> int:
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    target = ROOT / "build" / "fixtures_f5"
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

    failures: list[str] = []
    terlihat: list[str] = []

    def audit(*, tool, args, tool_context):
        terlihat.append(tool.name)
        return None

    cm = CodeMode(
        {"fs": toolset},
        limits=Limits(timeout_s=60, max_calls=30),
        direct_policy=DirectPolicy.ANNOTATIONS,
    )

    try:
        catalog = await cm.prepare()

        # --- 1. pemilahan
        direct = sorted(d.name for d in catalog.direct)
        sandboxed = {s.name for s in catalog.tools}
        print(f"tool di sandbox : {len(sandboxed)}")
        print(f"jalur direct    : {direct}")
        for d in catalog.direct:
            print(f"  - {d.name}: {d.reason}")
        if set(direct) != DESTRUKTIF:
            failures.append(f"pemilahan berubah: {direct} != {sorted(DESTRUKTIF)}")
        lolos = sandboxed & DESTRUKTIF
        if lolos:
            failures.append(f"tool destruktif lolos ke sandbox: {lolos}")

        # --- 2. tidak ada di deklarasi TypeScript
        bocor = [n for n in DESTRUKTIF if n in cm.declarations]
        print(f"bocor ke deklarasi: {bocor or '(tidak ada)'}")
        if bocor:
            failures.append(f"nama tool direct ada di deklarasi TS: {bocor}")

        # --- 3. tetap tersedia bagi model
        nama_direct = sorted(t.name for t in await cm.direct_toolset.get_tools())
        print(f"dideklarasikan ke model: {nama_direct}")
        if set(nama_direct) != DESTRUKTIF:
            failures.append(f"direct_toolset tidak lengkap: {nama_direct}")

        block = await cm.instruction_block()
        if "Tool yang dipanggil langsung" not in block:
            failures.append("blok instruction jalur direct tidak terbit")

        # --- konteks sungguhan
        agent = LlmAgent(
            name="gate5",
            model="gemini-2.0-flash",
            instruction=cm.instruction_provider("Kamu asisten berkas."),
            tools=cm.tools,
            before_tool_callback=audit,
        )
        svc = InMemorySessionService()
        session = await svc.create_session(app_name="f5", user_id="u1")
        ictx = InvocationContext(
            invocation_id="inv-1", agent=agent, session=session, session_service=svc
        )
        ctx = Context(ictx, function_call_id="fc-exec")

        # --- 4. jalur normal gagal di sandbox
        out = await cm.tool.run_async(
            args={"code": 'await tools.write_file({ path: "x.txt", content: "y" });'},
            tool_context=ctx,
        )
        print(f"\njalur normal    : status={out['status']} calls={out['tool_calls']}")
        print(f"  error         : {(out.get('error') or '')[:120]}")
        if out["status"] != "error" or out["tool_calls"] != 0:
            failures.append("tools.write_file dari kode TIDAK gagal")

        # --- 5. pemalsuan protokol ditolak host
        terlihat.clear()
        out2 = await cm.tool.run_async(args={"code": SUNTIKAN}, tool_context=ctx)
        print(f"pemalsuan       : status={out2['status']} calls={out2['tool_calls']}")
        print(f"  callback lihat: {terlihat or '(tidak ada)'}")
        if "write_file" in terlihat:
            failures.append("tool direct masuk rantai callback lewat pemalsuan")
        if (target / "diretas.txt").exists():
            failures.append("BERKAS TERTULIS - tool direct benar-benar berjalan")
    finally:
        await cm.close()

    print()
    if failures:
        print("GERBANG FASE 5a: GAGAL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GERBANG FASE 5a: LOLOS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
