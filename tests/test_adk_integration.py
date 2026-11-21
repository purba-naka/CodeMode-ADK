"""Gerbang Fase 3 - integrasi ADK.

Tanpa LLM: `ExecuteCodeTool.run_async` dipanggil langsung dengan ToolContext
sungguhan, persis seperti flow memanggilnya.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool

from codemode.adk import CodeMode, derive_tool_context
from codemode.sandbox import Limits

TRACE: list[str] = []


# --------------------------------------------------------------- tool palsu
def baca(path: str) -> dict:
    """Baca berkas.

    Args:
        path: lokasi berkas.
    """
    TRACE.append(f"baca({path})")
    return {"content": f"isi {path}"}


def daftar(path: str) -> dict:
    """Daftar isi direktori.

    Args:
        path: lokasi direktori.
    """
    TRACE.append(f"daftar({path})")
    return {"entries": [f"{path}/a", f"{path}/b", f"{path}/c"]}


def tulis_state(key: str, value: str) -> dict:
    """Tulis ke session state.

    Args:
        key: kunci.
        value: nilai.
    """
    return {"ok": True, "_marker": (key, value)}


def rusak(x: int) -> dict:
    """Selalu gagal.

    Args:
        x: diabaikan.
    """
    raise RuntimeError("upstream mati")


async def _ctx(agent: LlmAgent) -> Context:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="f3", user_id="u1")
    ictx = InvocationContext(
        invocation_id="inv-1", agent=agent, session=session, session_service=svc
    )
    return Context(ictx, function_call_id="fc-execute-code")


def _agent(cm: CodeMode, **kw: Any) -> LlmAgent:
    return LlmAgent(name="a", model="gemini-2.0-flash", tools=[cm.tool], **kw)


async def _run(cm: CodeMode, agent: LlmAgent, code: str) -> dict:
    ctx = await _ctx(agent)
    return await cm.tool.run_async(args={"code": code}, tool_context=ctx)


@pytest.fixture
def cm() -> CodeMode:
    TRACE.clear()
    return CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(daftar), FunctionTool(rusak)]},
        limits=Limits(timeout_s=30, max_calls=10),
    )


# -------------------------------------------------------------- jalur dasar
async def test_end_to_end_rantai(cm):
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
const d = await tools.daftar({ path: "/data" });
const f = await tools.baca({ path: d.entries[0] });
console.log("n:", d.entries.length, "| isi:", f.content);
""",
    )
    assert out["status"] == "done", out
    assert out["tool_calls"] == 2
    assert "n: 3 | isi: isi /data/a" in out["output"]
    assert TRACE == ["daftar(/data)", "baca(/data/a)"]


async def test_instruction_memuat_deklarasi(cm):
    block = await cm.instruction_block()
    assert "declare const tools" in block
    assert "baca: (input: BacaInput)" in block
    assert "console.log" in block


async def test_instruction_provider_async(cm):
    provider = cm.instruction_provider("Kamu asisten.")
    agent = LlmAgent(
        name="a", model="gemini-2.0-flash", tools=[cm.tool], instruction=provider
    )
    from google.adk.agents.readonly_context import ReadonlyContext

    text, bypass = await agent.canonical_instruction(ReadonlyContext(None))
    assert text.startswith("Kamu asisten.")
    assert "declare const tools" in text
    assert bypass is True


# ------------------------------------------------- rantai callback (S2b)
async def test_callback_jalan_untuk_tool_di_dalam_sandbox(cm):
    fired: list[str] = []

    def before(*, tool, args, tool_context):
        fired.append(f"before:{tool.name}")
        return None

    def after(*, tool, args, tool_context, tool_response):
        fired.append(f"after:{tool.name}")
        return None

    agent = _agent(cm, before_tool_callback=before, after_tool_callback=after)
    out = await _run(cm, agent, 'console.log((await tools.baca({path:"/x"})).content);')

    assert out["status"] == "done", out
    assert fired == ["before:baca", "after:baca"], (
        "callback tidak jalan untuk tool di dalam sandbox - audit per-tool mati"
    )


async def test_before_callback_bisa_membatalkan_tool(cm):
    """Dasar deny-rule / HITL."""

    def deny(*, tool, args, tool_context):
        if args.get("path", "").startswith("/rahasia"):
            return {"error": "ditolak kebijakan"}
        return None

    agent = _agent(cm, before_tool_callback=deny)
    out = await _run(
        cm,
        agent,
        """
const a = await tools.baca({ path: "/rahasia/x" });
console.log("hasil:", JSON.stringify(a));
""",
    )
    assert out["status"] == "done", out
    assert "ditolak kebijakan" in out["output"]
    assert TRACE == [], "tool tetap dijalankan padahal sudah ditolak"


async def test_after_callback_bisa_meredaksi(cm):
    def redact(*, tool, args, tool_context, tool_response):
        return {"content": "[DIREDAKSI]"}

    agent = _agent(cm, after_tool_callback=redact)
    code = 'console.log((await tools.baca({path:"/pii"})).content);'
    out = await _run(cm, agent, code)
    assert "[DIREDAKSI]" in out["output"]


async def test_kegagalan_tool_di_raise_ke_sandbox(cm):
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
try {
  await tools.rusak({ x: 1 });
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("raised:", e.name, "-", e.message);
}
""",
    )
    assert "BUG" not in out["output"]
    assert "raised: RuntimeError - upstream mati" in out["output"]


async def test_on_tool_error_mengubah_jadi_nilai(cm):
    def on_err(*, tool, args, tool_context, error):
        return {"ditangani": str(error)}

    agent = _agent(cm, on_tool_error_callback=on_err)
    out = await _run(
        cm,
        agent,
        """
const r = await tools.rusak({ x: 1 });
console.log("tidak raise:", JSON.stringify(r));
""",
    )
    assert out["status"] == "done", out
    assert "upstream mati" in out["output"]


# ------------------------------------------------------------- atribusi
async def test_function_call_id_berbeda_per_call():
    """Punya parent milik `execute_code`; tool dalam sandbox harus punya
    id sendiri, atau alur auth/konfirmasi salah atribusi."""
    agent = LlmAgent(name="a", model="gemini-2.0-flash")
    parent = await _ctx(agent)
    a = derive_tool_context(parent, call_id="1")
    b = derive_tool_context(parent, call_id="2")

    assert parent.function_call_id == "fc-execute-code"
    assert a.function_call_id == "fc-execute-code#1"
    assert b.function_call_id == "fc-execute-code#2"
    assert a.function_call_id != b.function_call_id


async def test_event_actions_dibagi_agar_state_tidak_hilang():
    """Jebakan senyap: kalau turunan membuat EventActions baru, state delta
    dan artifact delta dari tool dalam sandbox akan hilang, karena flow hanya
    mempersistensi EventActions milik `execute_code`."""
    agent = LlmAgent(name="a", model="gemini-2.0-flash")
    parent = await _ctx(agent)
    child = derive_tool_context(parent, call_id="1")

    assert child.actions is parent.actions

    child.state["dari_sandbox"] = "nilai"
    assert parent.actions.state_delta["dari_sandbox"] == "nilai"
    assert parent.state["dari_sandbox"] == "nilai"


# ------------------------------------------------------------- penolakan
async def test_tool_butuh_konfirmasi_ditolak_saat_build():
    tool = FunctionTool(baca, require_confirmation=True)
    cm = CodeMode({"fs": [tool]})
    catalog = await cm.prepare()

    assert catalog.tools == []
    assert len(catalog.rejected) == 1
    assert "require_confirmation" in catalog.rejected[0].reason
    assert "baca" not in cm.declarations


async def test_validasi_argumen_code(cm):
    agent = _agent(cm)
    for bad in [{}, {"code": ""}, {"code": "   "}, {"code": 123}]:
        ctx = await _ctx(agent)
        out = await cm.tool.run_async(args=bad, tool_context=ctx)
        assert out["status"] == "error"


async def test_nama_tool_salah_tidak_sampai_ke_bridge(cm):
    agent = _agent(cm)
    out = await _run(cm, agent, "await tools.tidak_ada({});")
    assert out["status"] == "error"
    assert "TypeError" in out.get("error", "")
    assert out["tool_calls"] == 0


# ---------------------------------------------------------------- laporan
async def test_report(cm):
    agent = _agent(cm)
    await _run(cm, agent, 'console.log("x");')
    rep = cm.report()
    assert rep["tools"] == 3
    assert rep["servers"] == ["fs"]
    assert rep["executions"] == 1
    assert rep["declaration_chars"] > 100


# ------------------------------------------- temuan Fase 3: isError MCP
def gagal_ala_mcp(path: str) -> dict:
    """Tiru envelope CallToolResult yang gagal.

    Args:
        path: lokasi berkas.
    """
    TRACE.append(f"gagal_ala_mcp({path})")
    return {
        "content": [{"type": "text", "text": f"ENOENT: no such file {path}"}],
        "isError": True,
    }


async def test_is_error_mcp_diubah_jadi_exception():
    """Terukur di server MCP sungguhan: `McpTool.run_async` TIDAK melempar
    exception saat server melaporkan kegagalan - hasilnya kembali sebagai
    nilai normal dengan `isError: true`.

    Di direct-mode itu tidak berbahaya. Di code mode fatal: kode sandbox
    membaca return value dan lanjut seolah berhasil.
    """
    cm = CodeMode({"fs": [FunctionTool(gagal_ala_mcp)]}, limits=Limits(timeout_s=30))
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
try {
  await tools.gagal_ala_mcp({ path: "/hilang" });
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("raised:", e.name, "|", e.message);
}
""",
    )
    assert out["status"] == "done", out
    assert "BUG" not in out["output"]
    assert "McpToolFailed" in out["output"]
    assert "ENOENT" in out["output"]


async def test_after_callback_bisa_membersihkan_is_error():
    """Raise terjadi SETELAH rantai callback, jadi callback redaksi masih
    bisa menganggap call ini berhasil - sama seperti direct-mode ADK, yang
    memperlakukan hasil ber-isError sebagai hasil sukses."""

    def sembuhkan(*, tool, args, tool_context, tool_response):
        return {"content": [{"type": "text", "text": "diperbaiki"}]}

    cm = CodeMode({"fs": [FunctionTool(gagal_ala_mcp)]}, limits=Limits(timeout_s=30))
    agent = _agent(cm, after_tool_callback=sembuhkan)
    out = await _run(
        cm,
        agent,
        """
const r = await tools.gagal_ala_mcp({ path: "/hilang" });
console.log("tidak raise:", r.content[0].text);
""",
    )
    assert out["status"] == "done", out
    assert "tidak raise: diperbaiki" in out["output"]
