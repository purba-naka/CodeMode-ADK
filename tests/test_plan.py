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
