"""Gerbang keluar Fase 3 - tumpukan penuh, MCP server sungguhan, tanpa LLM.

Kode yang dijalankan di bawah ini adalah kode yang SEHARUSNYA ditulis model.
LLM-nya belum dilibatkan - itu Fase 4. Yang diuji di sini: apakah seluruh
jalur dari ExecuteCodeTool -> sandbox -> RpcBridge -> McpTool -> server MCP
sungguhan benar-benar menyambung.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService

from codemode.adk import CodeMode
from codemode.sandbox import Limits

ROOT = Path(__file__).parent.parent

# Satu eksekusi: 3 jenis tool, filter di dalam kode, log hanya ringkasan.
# Inilah pola yang blog klaim unggul.
SKENARIO = """
// `structuredContent` adalah payload bertipe; `content` adalah blok MCP.
const tree = await tools.directory_tree({ path: "." });
const files = await tools.list_directory({ path: "." });

// saring DI DALAM kode - bukan di konteks model
const names = (files.structuredContent?.content ?? "")
  .split("\\n")
  .filter((l: string) => l.includes(".txt"))
  .map((l: string) => l.replace("[FILE]", "").trim())
  .filter(Boolean);

let totalBaris = 0;
const ringkasan: string[] = [];
for (const n of names) {
  const f = await tools.read_text_file({ path: n });
  const baris = (f.structuredContent?.content ?? "")
    .split("\\n")
    .map((b: string) => b.trim())
    .filter(Boolean);
  totalBaris += baris.length;
  if (baris.length > 2) ringkasan.push(`${n}=${baris.length}`);
}

// kegagalan tool dilempar sebagai exception, termasuk isError dari MCP
let errName = "(tidak ada)";
try {
  await tools.read_text_file({ path: "TIDAK_ADA.txt" });
  errName = "BUG-tidak-raise";
} catch (e) {
  errName = (e as Error).name;
}

console.log("berkas txt:", names.length);
console.log("total baris:", totalBaris);
console.log("yang >2 baris:", ringkasan.sort().join(", ") || "(tidak ada)");
console.log("isError jadi exception:", errName);
console.log("ukuran tree mentah:", JSON.stringify(tree).length, "char (tidak di-log)");
"""


async def main() -> int:
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    # Direktori fixture sendiri, dibersihkan tiap jalan. Sebelumnya gerbang
    # F1 dan F3 berbagi `accessible_files/`, jadi hasil F3 bergantung pada
    # urutan eksekusi - berkas sisa dari F1 mengubah hitungannya.
    target = ROOT / "build" / "fixtures_f3"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "kecil.txt").write_text("a\nb\n", encoding="utf-8")
    (target / "besar.txt").write_text("\n".join(f"baris {i}" for i in range(40)))
    (target / "sedang.txt").write_text("x\ny\nz\nw\n", encoding="utf-8")
    (target / "abaikan.md").write_text("# bukan txt\n", encoding="utf-8")

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

    audit: list[str] = []

    def before(*, tool, args, tool_context):
        audit.append(f"{tool.name}({list(args)}) fc={tool_context.function_call_id}")
        return None

    cm = CodeMode({"fs": toolset}, limits=Limits(timeout_s=60, max_calls=30))
    failures: list[str] = []

    try:
        await cm.prepare()
        agent = LlmAgent(
            name="gate3",
            model="gemini-2.0-flash",
            instruction=cm.instruction_provider("Kamu asisten berkas."),
            tools=[cm.tool],
            before_tool_callback=before,
        )

        svc = InMemorySessionService()
        session = await svc.create_session(app_name="f3", user_id="u1")
        ictx = InvocationContext(
            invocation_id="inv-1", agent=agent, session=session, session_service=svc
        )
        ctx = Context(ictx, function_call_id="fc-exec")

        rep = cm.report()
        print(f"tool             : {rep['tools']}")
        print(f"ditolak          : {len(rep['rejected'])}")
        print(f"deklarasi        : {rep['declaration_chars']} char")

        instruction, _ = await agent.canonical_instruction(ctx)
        print(
            f"instruction total: {len(instruction)} char "
            f"(~{len(instruction) // 4} token kasar)"
        )

        tool = agent.tools[0]
        out = await tool.run_async(args={"code": SKENARIO}, tool_context=ctx)

        print("\n--- hasil eksekusi")
        print(f"status      : {out['status']}")
        print(f"tool_calls  : {out['tool_calls']}")
        if out.get("error"):
            print(f"error       : {out['error'][:300]}")
        print("output:")
        for line in out["output"].splitlines():
            print(f"  | {line}")

        print(f"\n--- audit callback ({len(audit)} tool call terlihat)")
        for a in audit:
            print(f"  {a}")

        # ----- pemeriksaan -----
        if out["status"] != "done":
            failures.append(f"status={out['status']}")
        if out["tool_calls"] < 6:
            n = out["tool_calls"]
            failures.append(f"hanya {n} tool call, harap >=6")
        if len(audit) != out["tool_calls"]:
            failures.append(
                f"callback melihat {len(audit)} call, sandbox melakukan "
                f"{out['tool_calls']} - audit tidak lengkap"
            )
        if "berkas txt: 3" not in out["output"]:
            failures.append("filter di dalam kode salah (harap 3 berkas txt)")
        if "total baris: 46" not in out["output"]:
            failures.append("agregasi salah (harap 46 baris)")
        if "besar.txt=40" not in out["output"]:
            failures.append("ringkasan tidak memuat besar.txt=40")

        # atribusi: tiap call punya function_call_id sendiri
        ids = {a.split("fc=")[1] for a in audit}
        if len(ids) != len(audit):
            failures.append(f"function_call_id tidak unik per call: {ids}")
        if any(i == "fc-exec" for i in ids):
            failures.append("ada call yang memakai fc milik execute_code")

        # bukti penghematan konteks: tree mentah jauh lebih besar dari output
        print(f"\noutput ke model  : {len(out['output'])} char")
    finally:
        await cm.close()

    print()
    if failures:
        print("GERBANG FASE 3: GAGAL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GERBANG FASE 3: LOLOS")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GOOGLE_API_KEY", "dummy")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
