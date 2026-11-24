"""Satu-satunya daftar kopling ke internal ADK, dengan asersi yang berisik.

`RpcBridge._run` menduplikasi urutan callback `flows/llm_flows/functions.py`.
ADK tidak menyediakan hook, jadi duplikasi ini TIDAK bisa dihilangkan. Yang
bisa: kalau ADK berubah, gagal KERAS di pemakaian pertama, bukan senyap
melewati callback (audit/deny-rule/HITL/redaksi mati tanpa error).

Ini tidak membuktikan URUTAN callback masih benar - hanya bahwa nama yang
kita pakai masih ada. Urutan dijaga test integrasi.
"""

from __future__ import annotations

import inspect

from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.plugin_manager import PluginManager

PLUGIN_METHODS = (
    "run_before_tool_callback",
    "run_after_tool_callback",
    "run_on_tool_error_callback",
)
AGENT_ATTRS = (
    "canonical_before_tool_callbacks",
    "canonical_after_tool_callbacks",
    "canonical_on_tool_error_callbacks",
)
CONTEXT_KWARGS = ("event_actions", "function_call_id")

_verified = False


class AdkIncompatible(RuntimeError):
    pass


def verify(agent_cls: type | None = None) -> None:
    """Periksa semua nama yang dipakai bridge. Murah; hasilnya di-cache."""
    global _verified
    if _verified:
        return
    from google.adk.agents.llm_agent import LlmAgent

    problems: list[str] = []
    for m in PLUGIN_METHODS:
        if not callable(getattr(PluginManager, m, None)):
            problems.append(f"PluginManager.{m} hilang")
    for a in AGENT_ATTRS:
        if not hasattr(agent_cls or LlmAgent, a):
            problems.append(f"{(agent_cls or LlmAgent).__name__}.{a} hilang")
    params = inspect.signature(Context.__init__).parameters
    for k in CONTEXT_KWARGS:
        if k not in params:
            problems.append(f"Context.__init__ tidak lagi menerima `{k}`")
    for a in ("actions", "function_call_id"):
        if not hasattr(Context, a):
            problems.append(f"Context.{a} hilang")
    if "plugin_manager" not in InvocationContext.model_fields:
        problems.append("InvocationContext.plugin_manager hilang")
    if problems:
        raise AdkIncompatible(
            "versi ADK ini tidak cocok dengan RpcBridge: "
            + "; ".join(problems)
            + ". Callback akan DILEWATI bila dilanjutkan - tolak, jangan jalan."
        )
    _verified = True


def reset_for_tests() -> None:
    global _verified
    _verified = False
