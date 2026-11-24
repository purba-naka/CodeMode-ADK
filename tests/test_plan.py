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
