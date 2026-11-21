"""Diagnostik: berapa tool yang BENAR-BENAR dideklarasikan ADK ke model?

Pertanyaan ini menentukan sahih-tidaknya seluruh sumbu jumlah-tool di Fase 4.
Catatan kabel menunjukkan `n_tools_declared: 1` untuk code mode - itu memang
benar (`execute_code`). Tapi kalau direct mode juga hanya mengirim sedikit
tool, maka grid 10/30/60 tidak mengukur apa pun.

Dijawab tanpa LLM: `LlmRequest` dibangun dengan cara yang sama seperti flow,
lewat `tool.process_llm_request()`, lalu deklarasinya dihitung.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions import InMemorySessionService

from codemode.adk import CodeMode
from codemode.sandbox import Limits
from eval.harness import toolset


async def declared(agent: LlmAgent, ictx: InvocationContext) -> dict:
    """Bangun LlmRequest seperti flow, lalu hitung deklarasinya."""
    req = LlmRequest()
    ctx = Context(ictx, function_call_id="fc-diag")

    tools = await agent.canonical_tools(ReadonlyContext(ictx))
    for t in tools:
        await t.process_llm_request(tool_context=ctx, llm_request=req)

    decls = []
    for tool_block in req.config.tools or []:
        decls += list(getattr(tool_block, "function_declarations", None) or [])

    instruction, _ = await agent.canonical_instruction(ctx)

    return {
        "canonical_tools": len(tools),
        "declared": len(decls),
        "names": [d.name for d in decls][:6],
        "decl_chars": len(
            json.dumps(
                [
                    d.model_dump(exclude_none=True, by_alias=True, mode="json")
                    for d in decls
                ]
            )
        ),
        "instruction_chars": len(instruction),
    }


async def main() -> int:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="diag", user_id="u")

    print(f"{'tool':>5} {'schema':>7} {'mode':<7} {'decl':>5} "
          f"{'decl_char':>10} {'instr_char':>11} {'total_char':>11}")
    print("-" * 68)

    rows = []
    for schema in ("flat", "nested", "oneof"):
        for n in (10, 30, 60):
            for mode in ("direct", "code"):
                ts = toolset(n, schema)
                cm = None
                try:
                    if mode == "code":
                        cm = CodeMode(
                            {"orders": ts}, limits=Limits(timeout_s=30)
                        )
                        await cm.prepare()
                        agent = LlmAgent(
                            name="d",
                            model="gemini-2.0-flash",
                            instruction=cm.instruction_provider("Kamu analis."),
                            tools=[cm.tool],
                        )
                    else:
                        agent = LlmAgent(
                            name="d",
                            model="gemini-2.0-flash",
                            instruction="Kamu analis.",
                            tools=[ts],
                        )
                    ictx = InvocationContext(
                        invocation_id="i", agent=agent, session=session,
                        session_service=svc,
                    )
                    info = await declared(agent, ictx)
                    total = info["decl_chars"] + info["instruction_chars"]
                    rows.append((n, schema, mode, info, total))
                    print(f"{n:>5} {schema:>7} {mode:<7} "
                          f"{info['declared']:>5} {info['decl_chars']:>10} "
                          f"{info['instruction_chars']:>11} {total:>11}")
                finally:
                    if cm is not None:
                        await cm.close()
                    else:
                        await ts.close()

    fails = []
    for n, schema, mode, info, _total in rows:
        if mode == "direct" and info["declared"] != n:
            fails.append(
                f"direct {n}/{schema}: hanya {info['declared']} "
                "dideklarasikan -> sumbu jumlah-tool TIDAK mengukur apa pun"
            )
        if mode == "code" and info["declared"] != 1:
            fails.append(
                f"code {n}/{schema}: {info['declared']} dideklarasikan, "
                "harusnya 1 (execute_code)"
            )

    print("\n--- total konteks (deklarasi + instruction), apel-ke-apel")
    print(f"{'schema':>7} {'tool':>5} {'direct':>10} {'code':>10} {'rasio':>7}")
    out = []
    for schema in ("flat", "nested", "oneof"):
        for n in (10, 30, 60):
            d = next(
                t for x, s, m, _i, t in rows
                if x == n and s == schema and m == "direct"
            )
            c = next(
                t for x, s, m, _i, t in rows
                if x == n and s == schema and m == "code"
            )
            print(f"{schema:>7} {n:>5} {d:>10} {c:>10} {c / d:>6.2f}x")
            out.append({
                "schema": schema, "n_tools": n,
                "direct_chars": d, "code_chars": c,
                "ratio": round(c / d, 4),
            })
    Path("build/context_cost_declared.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    print("\nditulis ke build/context_cost_declared.json")

    if fails:
        print("\nMASALAH:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nkedua mode mendeklarasikan jumlah tool yang diharapkan")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GOOGLE_API_KEY", "dummy")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
