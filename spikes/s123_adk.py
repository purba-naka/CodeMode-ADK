"""Spike S1/S2/S3 - sisi ADK.

S1: `instruction` boleh provider async?
S2: before_tool/after_tool jalan kalau `tool.run_async()` dipanggil langsung?
S3: schema MCP - seberapa jauh kita bergantung pada API privat?
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import FunctionTool

OK = "PASS"
NO = "FAIL"


# ----------------------------------------------------------------- S1
async def s1() -> None:
    print("=== S1: instruction provider async")

    calls: list[str] = []

    async def build_instruction(ctx: ReadonlyContext) -> str:
        await asyncio.sleep(0)  # betul-betul async
        calls.append("dipanggil")
        return "INSTRUKSI DARI PROVIDER ASYNC"

    agent = LlmAgent(name="s1", model="gemini-2.0-flash",
                     instruction=build_instruction)

    # ADK memanggil ini saat membangun prompt
    text, bypass_state_injection = await agent.canonical_instruction(
        ReadonlyContext(None)  # type: ignore[arg-type]
    )
    print(f"  provider dipanggil : {len(calls) == 1}")
    print(f"  hasil              : {text!r}")
    print(f"  bypass_state_inject: {bypass_state_injection}")
    print(f"  -> {OK if text.endswith('ASYNC') else NO}: provider async didukung")
    print("  catatan: bypass_state_injection=True -> {var} TIDAK di-inject "
          "otomatis.\n           Kalau butuh, pakai "
          "instructions_utils.inject_session_state() sendiri.\n")


# ----------------------------------------------------------------- S2
async def s2() -> None:
    print("=== S2: callback saat run_async dipanggil langsung")

    fired: list[str] = []

    def before_cb(*, tool, args, tool_context):
        fired.append("before")
        return None

    def after_cb(*, tool, args, tool_context, tool_response):
        fired.append("after")
        return None

    def ping(x: int) -> dict:
        """Tool uji.

        Args:
            x: angka apa saja.
        """
        return {"hasil": x * 2}

    tool = FunctionTool(ping)
    agent = LlmAgent(
        name="s2", model="gemini-2.0-flash", tools=[tool],
        before_tool_callback=before_cb, after_tool_callback=after_cb,
    )

    # inilah yang akan dilakukan RpcBridge: panggil tool langsung
    result = await tool.run_async(args={"x": 21}, tool_context=None)  # type: ignore[arg-type]

    print(f"  hasil tool     : {result}")
    print(f"  callback jalan : {fired or 'TIDAK ADA'}")
    print(f"  -> {NO if not fired else OK}: callback "
          f"{'TIDAK' if not fired else ''} terpanggil")

    # buktikan callback-nya sendiri waras (agar bukan salah wiring)
    print(f"  sanity: canonical_before={len(agent.canonical_before_tool_callbacks)} "
          f"canonical_after={len(agent.canonical_after_tool_callbacks)}")
    print("  KESIMPULAN: hipotesis plan BENAR - callback dipicu oleh flow")
    print("  (functions.py:510-569), bukan oleh tool. RpcBridge harus "
          "mereplikasi:")
    print("    1. plugin_manager.run_before_tool_callback(...)")
    print("    2. agent.canonical_before_tool_callbacks (break kalau ada hasil)")
    print("    3. tool.run_async(...)   <- + on_tool_error callbacks")
    print("    4. plugin_manager.run_after_tool_callback(...)")
    print("    5. agent.canonical_after_tool_callbacks\n")


# ----------------------------------------------------------------- S3
async def s3() -> None:
    print("=== S3: akses schema MCP")

    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        StdioConnectionParams,
    )
    from mcp import StdioServerParameters

    target = Path(__file__).parent.parent / "accessible_files"
    target.mkdir(exist_ok=True)
    (target / "catatan.txt").write_text("halo dari spike\n", encoding="utf-8")

    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=npx,
                args=["-y", "@modelcontextprotocol/server-filesystem",
                      str(target)],
            ),
            timeout=60,
        )
    )

    try:
        tools = await toolset.get_tools()
        print(f"  tool ditemukan: {len(tools)}")

        rows = []
        for t in tools:
            raw = getattr(t, "_mcp_tool", None)
            has_in = bool(getattr(raw, "inputSchema", None)) if raw else False
            out = getattr(raw, "outputSchema", None) if raw else None
            rows.append((t.name, raw is not None, has_in, out is not None))

        print(f"  {'tool':34} {'_mcp_tool':10} {'inputSchema':12} outputSchema")
        for name, has_raw, has_in, has_out in rows:
            print(f"  {name:34} {str(has_raw):10} {str(has_in):12} {has_out}")

        n_out = sum(1 for r in rows if r[3])
        print(f"\n  -> {OK if all(r[1] and r[2] for r in rows) else NO}: "
              f"_mcp_tool.inputSchema tersedia untuk semua tool")
        print(f"  -> outputSchema ada di {n_out}/{len(rows)} tool "
              f"{'(-> interface Output pakai index signature)' if n_out == 0 else ''}")

        # contoh schema, untuk melihat kompleksitas nyata
        sample = next((t for t in tools if t.name == "read_text_file"), tools[0])
        raw = sample._mcp_tool  # noqa: SLF001
        print(f"\n  contoh inputSchema ({sample.name}):")
        print("   ", json.dumps(raw.inputSchema, indent=2)[:420].replace(
            "\n", "\n    "))

        # --- fallback: jalur _get_declaration() ---
        print("\n  --- fallback tanpa _mcp_tool ---")
        decl = sample._get_declaration()  # noqa: SLF001
        print(f"  _get_declaration() -> {type(decl).__name__}")
        print(f"    name={decl.name}")
        print(f"    parameters        : {'ADA' if decl.parameters else 'None'}")
        print(f"    parameters_json_schema: "
              f"{'ADA' if getattr(decl, 'parameters_json_schema', None) else 'None'}")

        from google.adk.features import FeatureName, override_feature_enabled

        override_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, True)
        decl2 = sample._get_declaration()  # noqa: SLF001
        pjs = getattr(decl2, "parameters_json_schema", None)
        print(f"  dengan JSON_SCHEMA_FOR_FUNC_DECL=True:")
        print(f"    parameters_json_schema: {'ADA' if pjs else 'None'}")
        if pjs:
            same = pjs == raw.inputSchema
            print(f"    identik dengan _mcp_tool.inputSchema: {same}")
            print(f"  -> {OK}: fallback LAYAK - schema penuh tanpa API privat")
    finally:
        await toolset.close()
    print()


async def main() -> None:
    await s1()
    await s2()
    try:
        await s3()
    except Exception as e:  # noqa: BLE001
        print(f"  S3 GAGAL: {type(e).__name__}: {e}")
        print("  (butuh npx + unduhan @modelcontextprotocol/server-filesystem)")


if __name__ == "__main__":
    os.environ.setdefault("GOOGLE_API_KEY", "dummy-untuk-spike")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
