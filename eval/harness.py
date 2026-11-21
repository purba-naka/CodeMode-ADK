"""Harness evaluasi Fase 4 - direct-mode vs code-mode.

Dua bagian, sengaja dipisah karena biayanya beda jauh:

  bagian A (tanpa LLM) : biaya konteks di seluruh grid. Instan, deterministik.
                         Menjawab separuh klaim 1 & 2 blog.
  bagian B (butuh LLM) : correctness + akurasi pemilihan tool. Mahal.

    python eval/harness.py context                 # bagian A
    python eval/harness.py run --tools 10,30,60    # bagian B
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from codemode.adk import CodeMode
from codemode.catalog import build_catalog, enable_json_schema_feature
from codemode.codegen import typescript
from codemode.sandbox import Limits
from eval.tasks import TASKS, Task

PY = sys.executable
MODEL = os.environ.get("EVAL_MODEL", "gemini-3.5-flash")
SCHEMAS = ("flat", "nested", "oneof")
CORE = {
    "list_regions",
    "get_orders_by_region",
    "get_order_detail",
    "get_region_summary",
    "poll_deploy_status",
}


def toolset(n_tools: int, schema: str) -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=PY,
                args=[
                    str(ROOT / "eval" / "mock_server.py"),
                    "--tools",
                    str(n_tools),
                    "--schema",
                    schema,
                ],
            ),
            timeout=60,
        )
    )


# ===================================================================== A
@dataclass
class ContextCost:
    n_tools: int
    schema: str
    direct_chars: int
    code_chars: int

    @property
    def ratio(self) -> float:
        return self.code_chars / self.direct_chars if self.direct_chars else 0.0


async def measure_context(n_tools: int, schema: str) -> ContextCost:
    """Biaya konteks kedua mode, tanpa memanggil LLM.

    direct : total JSON schema seluruh tool (yang dikirim sebagai tool defs)
    code   : deklarasi TS + prompt pembungkus (yang masuk instruction)
    """
    enable_json_schema_feature()
    ts = toolset(n_tools, schema)
    try:
        catalog = await build_catalog({"orders": ts})
        direct = sum(
            len(json.dumps(t.input_schema)) + len(t.description) + len(t.name)
            for t in catalog.tools
        )
        code = len(typescript.render(catalog))
    finally:
        await ts.close()
    return ContextCost(n_tools, schema, direct, code)


async def part_a(grid: list[int]) -> list[ContextCost]:
    rows: list[ContextCost] = []
    print(f"{'tool':>5} {'schema':>7} {'direct':>9} {'code':>9} {'rasio':>7}")
    print("-" * 42)
    for schema in SCHEMAS:
        for n in grid:
            c = await measure_context(n, schema)
            rows.append(c)
            print(
                f"{c.n_tools:>5} {c.schema:>7} {c.direct_chars:>9} "
                f"{c.code_chars:>9} {c.ratio:>6.2f}x"
            )
    return rows


# ===================================================================== B
@dataclass
class Run:
    task: str
    kind: str
    mode: str
    n_tools: int
    schema: str
    ok: bool
    in_tokens: int
    out_tokens: int
    turns: int
    tool_calls: int
    tools_used: list[str] = field(default_factory=list)
    distractors: int = 0
    wall_ms: float = 0.0
    answer: str = ""
    error: str = ""

    @property
    def picked_right(self) -> bool:
        """Akurasi pemilihan tool: tool inti yang diharapkan benar-benar
        dipakai, dan tidak terpancing ke pengecoh."""
        return self.distractors == 0 and bool(self.tools_used)


async def run_one(
    task: Task, mode: str, n_tools: int, schema: str, timeout: float = 150.0
) -> Run:
    used: list[str] = []

    def before(*, tool, args, tool_context):
        if tool.name != "execute_code":
            used.append(tool.name)
        return None

    ts = toolset(n_tools, schema)
    cm: CodeMode | None = None
    t0 = time.perf_counter()

    if mode == "code":
        cm = CodeMode({"orders": ts}, limits=Limits(timeout_s=90, max_calls=40))
        await cm.prepare()
        agent = LlmAgent(
            name="eval_code",
            model=MODEL,
            instruction=cm.instruction_provider(
                "Kamu analis data pesanan. Jawab ringkas dan sebutkan angkanya."
            ),
            tools=[cm.tool],
            before_tool_callback=before,
        )
    else:
        agent = LlmAgent(
            name="eval_direct",
            model=MODEL,
            instruction=(
                "Kamu analis data pesanan. Pakai tool yang tersedia. "
                "Jawab ringkas dan sebutkan angkanya."
            ),
            tools=[ts],
            before_tool_callback=before,
        )

    svc = InMemorySessionService()
    session = await svc.create_session(app_name="eval", user_id="u")
    runner = Runner(agent=agent, app_name="eval", session_service=svc)

    tin = tout = turns = 0
    answer, error = "", ""

    try:

        async def drive() -> None:
            nonlocal tin, tout, turns, answer
            msg = types.Content(role="user", parts=[types.Part(text=task.prompt)])
            async for ev in runner.run_async(
                user_id="u", session_id=session.id, new_message=msg
            ):
                u = getattr(ev, "usage_metadata", None)
                if u:
                    turns += 1
                    tin += u.prompt_token_count or 0
                    tout += u.candidates_token_count or 0
                if ev.is_final_response() and ev.content and ev.content.parts:
                    answer = "".join(p.text or "" for p in ev.content.parts)

        await asyncio.wait_for(drive(), timeout=timeout)
    except TimeoutError:
        error = f"timeout {timeout}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:300]
    finally:
        if cm is not None:
            await cm.close()
        else:
            await ts.close()

    return Run(
        task=task.id,
        kind=task.kind,
        mode=mode,
        n_tools=n_tools,
        schema=schema,
        ok=bool(answer) and task.check(answer),
        in_tokens=tin,
        out_tokens=tout,
        turns=turns,
        tool_calls=len(used),
        tools_used=used,
        distractors=sum(1 for t in used if t not in CORE),
        wall_ms=(time.perf_counter() - t0) * 1000,
        answer=answer.strip()[:300],
        error=error,
    )


async def part_b(
    grid: list[int], schema: str, task_ids: list[str] | None, repeats: int
) -> list[Run]:
    tasks = [t for t in TASKS if not task_ids or t.id in task_ids]
    rows: list[Run] = []
    total = len(tasks) * 2 * len(grid) * repeats
    i = 0
    for n in grid:
        for t in tasks:
            for mode in ("direct", "code"):
                for _ in range(repeats):
                    i += 1
                    r = await run_one(t, mode, n, schema)
                    rows.append(r)
                    flag = "OK " if r.ok else "SALAH"
                    print(
                        f"[{i:>3}/{total}] {n:>2}t {t.id:>3} {mode:<6} {flag} "
                        f"in={r.in_tokens:>6} out={r.out_tokens:>4} "
                        f"turn={r.turns} call={r.tool_calls} "
                        f"dist={r.distractors} {r.wall_ms / 1000:>5.1f}s"
                        + (f" !{r.error[:60]}" if r.error else "")
                    )
    return rows


def summarise(rows: list[Run]) -> None:
    def agg(sel: list[Run]) -> dict[str, Any]:
        if not sel:
            return {}
        return {
            "n": len(sel),
            "benar": f"{sum(r.ok for r in sel)}/{len(sel)}",
            "in_median": int(statistics.median(r.in_tokens for r in sel)),
            "out_median": int(statistics.median(r.out_tokens for r in sel)),
            "turn_median": statistics.median(r.turns for r in sel),
            "call_median": statistics.median(r.tool_calls for r in sel),
            "pengecoh": sum(r.distractors for r in sel),
        }

    print("\n=== per mode x jumlah tool")
    print(
        f"{'tool':>5} {'mode':<7} {'benar':>7} {'in':>8} {'out':>6} "
        f"{'turn':>5} {'call':>5} {'pengecoh':>9}"
    )
    for n in sorted({r.n_tools for r in rows}):
        for mode in ("direct", "code"):
            a = agg([r for r in rows if r.n_tools == n and r.mode == mode])
            if a:
                print(
                    f"{n:>5} {mode:<7} {a['benar']:>7} {a['in_median']:>8} "
                    f"{a['out_median']:>6} {a['turn_median']:>5} "
                    f"{a['call_median']:>5} {a['pengecoh']:>9}"
                )

    print("\n=== per kategori task")
    print(f"{'kind':<9} {'mode':<7} {'benar':>7} {'in':>8} {'turn':>5}")
    for kind in ("single", "chain", "data", "control"):
        for mode in ("direct", "code"):
            a = agg([r for r in rows if r.kind == kind and r.mode == mode])
            if a:
                print(
                    f"{kind:<9} {mode:<7} {a['benar']:>7} "
                    f"{a['in_median']:>8} {a['turn_median']:>5}"
                )

    print("\n=== gerbang promosi")
    for n in sorted({r.n_tools for r in rows}):
        d = [r for r in rows if r.n_tools == n and r.mode == "direct"]
        c = [r for r in rows if r.n_tools == n and r.mode == "code"]
        if not d or not c:
            continue
        acc_d = sum(r.ok for r in d) / len(d)
        acc_c = sum(r.ok for r in c) / len(c)
        tok_d = statistics.median(r.in_tokens for r in d)
        tok_c = statistics.median(r.in_tokens for r in c)
        verdict = (
            "LOLOS"
            if acc_c >= acc_d and tok_c < tok_d
            else "correctness turun"
            if acc_c < acc_d
            else "token tidak hemat"
        )
        print(
            f"  {n:>2} tool: akurasi {acc_d:.0%} -> {acc_c:.0%} | "
            f"token {tok_d} -> {tok_c} "
            f"({(tok_c / tok_d - 1) * 100:+.0f}%) | {verdict}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["context", "run"])
    ap.add_argument("--tools", default="10,30,60")
    ap.add_argument("--schema", default="flat", choices=SCHEMAS)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="")
    ns = ap.parse_args()

    grid = [int(x) for x in ns.tools.split(",")]
    ids = [x.strip() for x in ns.tasks.split(",") if x.strip()] or None

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    if ns.part == "context":
        rows = asyncio.run(part_a(grid))
        payload = [asdict(r) | {"ratio": round(r.ratio, 4)} for r in rows]
    else:
        if not os.environ.get("GOOGLE_API_KEY"):
            print("GOOGLE_API_KEY belum diset", file=sys.stderr)
            return 2
        print(f"model: {MODEL} | schema: {ns.schema} | grid: {grid}\n")
        runs = asyncio.run(part_b(grid, ns.schema, ids, ns.repeats))
        summarise(runs)
        payload = [asdict(r) for r in runs]

    if ns.out:
        Path(ns.out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nditulis ke {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
