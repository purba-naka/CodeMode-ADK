"""Spike S2b - perbaikan S2.

S2 membuktikan `tool.run_async()` langsung TIDAK memicu callback.
S2b menguji apakah rantai callback bisa direplikasi dari dalam sebuah tool,
yaitu posisi `RpcBridge` nanti.

Yang harus dibuktikan:
  1. `invocation_context` terjangkau dari ToolContext
  2. `agent` + `plugin_manager` terjangkau darinya
  3. rantai 5 langkah berjalan, dan URUTANNYA benar
  4. before-callback yang mengembalikan nilai BISA membatalkan tool (dasar HITL)
  5. on_tool_error callback jalan saat tool melempar exception
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import os
import sys

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool

TRACE: list[str] = []


# --------------------------------------------------------------- tool uji
def ambil_data(path: str) -> dict:
    """Tool dalam sandbox.

    Args:
        path: lokasi berkas.
    """
    TRACE.append(f"TOOL ambil_data({path})")
    return {"isi": f"data dari {path}"}


def tool_rusak(x: int) -> dict:
    """Tool yang selalu gagal.

    Args:
        x: diabaikan.
    """
    TRACE.append("TOOL tool_rusak -> raise")
    raise RuntimeError("upstream MCP mati")


# ------------------------------------------- inilah calon RpcBridge.call()
async def bridge_call(tool, args: dict, tool_context: Context):
    """Replikasi flows/llm_flows/functions.py:510-569."""
    ictx: InvocationContext = tool_context._invocation_context  # noqa: SLF001
    agent = ictx.agent
    pm = ictx.plugin_manager

    # ADK men-deepcopy args sebelum callback (functions.py:482) karena callback
    # boleh memutasinya. Argumen dari sandbox juga harus diisolasi.
    args = copy.deepcopy(args)

    # Langkah 1: plugin before
    resp = await pm.run_before_tool_callback(
        tool=tool, tool_args=args, tool_context=tool_context
    )
    if resp is not None:
        TRACE.append("plugin-before MENIMPA")

    # Langkah 2: canonical before
    if resp is None:
        for cb in agent.canonical_before_tool_callbacks:
            resp = cb(tool=tool, args=args, tool_context=tool_context)
            if inspect.isawaitable(resp):
                resp = await resp
            if resp:
                TRACE.append("canonical-before MENIMPA -> tool dibatalkan")
                break

    # Langkah 3: jalankan tool
    if resp is None:
        try:
            resp = await tool.run_async(args=args, tool_context=tool_context)
        except Exception as err:  # noqa: BLE001
            TRACE.append(f"tool RAISE: {type(err).__name__}")
            # URUTAN PENTING: ADK menjalankan plugin DULU, lalu canonical
            # (functions.py:454-477). Jangan dibalik.
            handled = await pm.run_on_tool_error_callback(
                tool=tool, tool_args=args,
                tool_context=tool_context, error=err,
            )
            if handled is None:
                for cb in agent.canonical_on_tool_error_callbacks:
                    handled = cb(tool=tool, args=args,
                                 tool_context=tool_context, error=err)
                    if inspect.isawaitable(handled):
                        handled = await handled
                    if handled is not None:
                        TRACE.append("on_tool_error MENANGANI")
                        break
            if handled is None:
                raise          # <- WAJIB: sandbox harus melihat exception
            resp = handled

    # Langkah 4: plugin after
    altered = await pm.run_after_tool_callback(
        tool=tool, tool_args=args, tool_context=tool_context, result=resp
    )

    # Langkah 5: canonical after
    if altered is None:
        for cb in agent.canonical_after_tool_callbacks:
            altered = cb(tool=tool, args=args, tool_context=tool_context,
                         tool_response=resp)
            if inspect.isawaitable(altered):
                altered = await altered
            if altered:
                break

    return altered if altered is not None else resp


# ------------------------------------------------------------------ setup
async def skenario(nama: str, agent: LlmAgent, tool, args: dict,
                  expect_raise: bool = False) -> None:
    TRACE.clear()
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="s2b", user_id="u1")
    ictx = InvocationContext(
        invocation_id="inv-1", agent=agent, session=session,
        session_service=svc,
    )
    tctx = Context(ictx)

    print(f"--- {nama}")
    print(f"    invocation_context terjangkau: "
          f"{tctx._invocation_context is ictx}")  # noqa: SLF001
    print(f"    agent terjangkau             : "
          f"{ictx.agent.name!r}")
    print(f"    plugin_manager terjangkau    : "
          f"{type(ictx.plugin_manager).__name__}")

    try:
        hasil = await bridge_call(tool, args, tctx)
        print(f"    hasil: {hasil}")
        if expect_raise:
            print("    -> FAIL: seharusnya raise")
    except Exception as e:  # noqa: BLE001
        print(f"    RAISE ke pemanggil: {type(e).__name__}: {e}")
        print(f"    -> {'PASS' if expect_raise else 'FAIL'}: "
              f"exception {'diteruskan' if expect_raise else 'tak terduga'}")

    print(f"    urutan: {' > '.join(TRACE) if TRACE else '(kosong)'}\n")


async def main() -> None:
    tool_ok = FunctionTool(ambil_data)
    tool_bad = FunctionTool(tool_rusak)

    # ---- kasus 1: rantai lengkap, callback harus jalan
    def before_cb(*, tool, args, tool_context):
        TRACE.append(f"before({tool.name})")
        return None

    def after_cb(*, tool, args, tool_context, tool_response):
        TRACE.append(f"after({tool.name})")
        return None

    a1 = LlmAgent(name="a1", model="gemini-2.0-flash",
                  tools=[tool_ok, tool_bad],
                  before_tool_callback=before_cb,
                  after_tool_callback=after_cb)
    await skenario("1. rantai lengkap (before > tool > after)",
                   a1, tool_ok, {"path": "/x"})

    # ---- kasus 2: before menimpa -> dasar HITL / deny
    def deny_cb(*, tool, args, tool_context):
        TRACE.append(f"before({tool.name}) MENOLAK")
        return {"error": "ditolak oleh kebijakan"}

    a2 = LlmAgent(name="a2", model="gemini-2.0-flash", tools=[tool_ok],
                  before_tool_callback=deny_cb, after_tool_callback=after_cb)
    await skenario("2. before menimpa -> tool TIDAK dijalankan (dasar HITL)",
                   a2, tool_ok, {"path": "/rahasia"})

    # ---- kasus 3: after mengubah hasil (dasar redaksi/PII)
    def redact_cb(*, tool, args, tool_context, tool_response):
        TRACE.append("after MEREDAKSI")
        return {"isi": "[DIREDAKSI]"}

    a3 = LlmAgent(name="a3", model="gemini-2.0-flash", tools=[tool_ok],
                  before_tool_callback=before_cb, after_tool_callback=redact_cb)
    await skenario("3. after mengubah hasil (dasar redaksi PII)",
                   a3, tool_ok, {"path": "/pii"})

    # ---- kasus 4: tool gagal -> harus RAISE ke sandbox
    a4 = LlmAgent(name="a4", model="gemini-2.0-flash", tools=[tool_bad],
                  before_tool_callback=before_cb, after_tool_callback=after_cb)
    await skenario("4. tool gagal -> exception WAJIB diteruskan ke sandbox",
                   a4, tool_bad, {"x": 1}, expect_raise=True)

    # ---- kasus 5: on_tool_error menangani -> TIDAK raise ke sandbox
    def on_err_cb(*, tool, args, tool_context, error):
        return {"error": f"ditangani: {error}"}

    a5 = LlmAgent(name="a5", model="gemini-2.0-flash", tools=[tool_bad],
                  before_tool_callback=before_cb,
                  after_tool_callback=after_cb,
                  on_tool_error_callback=on_err_cb)
    await skenario("5. on_tool_error menangani -> jadi nilai, bukan exception",
                   a5, tool_bad, {"x": 1})


if __name__ == "__main__":
    os.environ.setdefault("GOOGLE_API_KEY", "dummy-untuk-spike")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
