"""Gerbang F6-1: keputusan dibuat sekali di Plan, hilir hanya membaca."""

import dataclasses

import pytest
from google.adk.tools import FunctionTool

from codemode.sandbox import Limits
from codemode.adk import CodeMode


def alfa(x: str) -> dict:
    """a.

    Args:
        x: x.
    """
    return {"x": x}


def beta(x: str) -> dict:
    """b.

    Args:
        x: x.
    """
    return {"x": x}


async def _plan(**kw):
    cm = CodeMode({"s1": [FunctionTool(alfa)], "s2": [FunctionTool(beta)]},
                  limits=Limits(timeout_s=30), **kw)
    await cm.prepare()
    return cm


async def test_plan_imutabel():
    cm = await _plan()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cm.plan.flat = True  # type: ignore[misc]
    assert isinstance(cm.plan.code_tools, tuple)


async def test_flat_satu_sumber_untuk_deklarasi_dan_manifest():
    for override in (None, True, False):
        cm = await _plan(flat=override)
        flat = cm.plan.flat
        # deklarasi TS: flat => `tools.alfa`, tidak flat => `tools.s1.alfa`
        assert ("s1:" in cm.declarations) is (not flat), (override, flat)
        assert cm.plan.manifest() == {"s1": ["alfa"], "s2": ["beta"]}


async def test_explain_memuat_keputusan():
    cm = await _plan()
    teks = cm.plan.explain()
    assert "s1.alfa" in teks and "flat=" in teks


# ------------------------------------------ F6-2: tipe janji == nilai kirim
import json  # noqa: E402

from tests.test_adk_integration import _agent, _RawSnake, _run  # noqa: E402


class _McpTiruan(FunctionTool):
    async def run_async(self, *, args, tool_context):
        return {
            "content": [{"type": "text", "text": "hai"}],
            "structuredContent": {"result": "hai"},
        }


def baca_mcp(full_name: str) -> dict:
    """tiruan.

    Args:
        full_name: nama.
    """
    return {}


async def _nilai_di_sandbox(cm) -> object:
    out = await _run(
        cm,
        _agent(cm),
        'console.log(JSON.stringify(await tools.baca_mcp({ full_name: "a.b" })));',
    )
    assert out["status"] == "done", out
    return json.loads(out["output"].strip().splitlines()[-1])


def _cm_mcp() -> CodeMode:
    tool = _McpTiruan(baca_mcp)
    tool._mcp_tool = _RawSnake()
    return CodeMode({"srv": [tool]}, limits=Limits(timeout_s=30))


async def test_nilai_di_sandbox_memenuhi_tipe_yang_dijanjikan():
    from codemode.result import adapters_for

    cm = _cm_mcp()
    cat = await cm.prepare()
    nilai = await _nilai_di_sandbox(cm)
    ad = adapters_for(cat)[("srv", "baca_mcp")]
    assert ad.declared_schema() is not None
    assert ad.conformance_errors(nilai) == []


async def test_sabotase_deliver_tanpa_ubah_tipe_ketahuan(monkeypatch):
    """Intinya F6-2: kelasnya hilang, bukan satu instansnya. Kalau deliver()
    membuka envelope tapi tipe tetap menjanjikan envelope, test ini gagal."""
    from codemode.result import ResultAdapter, adapters_for

    monkeypatch.setattr(
        ResultAdapter, "deliver", lambda self, raw: raw["structuredContent"]
    )
    cm = _cm_mcp()
    cat = await cm.prepare()
    nilai = await _nilai_di_sandbox(cm)
    ad = adapters_for(cat)[("srv", "baca_mcp")]
    assert ad.conformance_errors(nilai) != []


# --------------------------------------------------- F6-4: CallRecord
from tests.test_adk_integration import argumen_buruk  # noqa: E402


async def test_call_record_lengkap_dan_bersarang():
    cm = CodeMode(
        {"s": [FunctionTool(alfa), FunctionTool(argumen_buruk)]},
        limits=Limits(timeout_s=30),
    )
    await _run(
        cm,
        _agent(cm),
        """
await tools.alfa({ x: "satu" });
await Promise.all([tools.alfa({ x: "a" }), tools.alfa({ x: "b" })]);
try { await tools.argumen_buruk({ path: "p" }); } catch (_) {}
""",
    )
    rec = cm.calls
    assert len(rec) == 4
    assert {r.depth for r in rec} == {1}
    assert all(r.parent_call_id and r.call_id.startswith(r.parent_call_id) for r in rec)
    assert len({r.call_id for r in rec}) == 4  # identitas unik, paralel pun
    gagal = [r for r in rec if not r.ok]
    assert [(g.tool, g.failure_kind) for g in gagal] == [("argumen_buruk", "adk_error_dict")]
    assert all(r.bytes_out > 0 for r in rec if r.ok)


def test_pencatat_naif_dari_callback_salah_pada_call_bersarang():
    """Gerbang yang sengaja mendemonstrasikan bug lama (mcpcost.py awal):
    `after` menulis ke entri TERAKHIR. Pada urutan bersarang
    before(exec) > before(q) > after(q) > after(exec), entri query_table
    tertimpa hasil execute_code. CallRecord tidak punya kelas bug ini."""
    jejak: list[dict] = []

    def before(nama):
        jejak.append({"tool": nama, "bytes": 0})

    def after(ukuran):
        jejak[-1]["bytes"] = ukuran  # asumsi "tidak bersarang" - SALAH

    before("execute_code")
    before("query_table")
    after(18000)  # hasil MCP sesungguhnya
    after(120)  # hasil execute_code (kecil)
    byte_mcp_naif = sum(j["bytes"] for j in jejak if j["tool"] == "query_table")
    assert byte_mcp_naif == 120  # bukan 18000: bug lama terdemonstrasi


# --------------------------------------------------- F6-5: kopling ADK
def test_adk_terpasang_cocok_dengan_bridge():
    from codemode.adk import adk_compat

    adk_compat.reset_for_tests()
    adk_compat.verify()


def test_adk_berubah_gagal_keras_bukan_senyap(monkeypatch):
    """Sabotase: ADK 'mengganti nama' salah satu atribut callback. Harus
    ditolak saat dipakai, bukan membuat callback dilewati tanpa suara."""
    from google.adk.agents.llm_agent import LlmAgent

    from codemode.adk import adk_compat

    monkeypatch.delattr(LlmAgent, "canonical_after_tool_callbacks")
    adk_compat.reset_for_tests()
    try:
        with pytest.raises(adk_compat.AdkIncompatible, match="canonical_after"):
            adk_compat.verify()
    finally:
        adk_compat.reset_for_tests()
