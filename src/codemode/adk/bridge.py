"""RpcBridge - satu-satunya lapisan yang menyentuh ADK.

Tugasnya: menerima tool call dari sandbox dan menjalankannya lewat ADK
**persis seperti flow melakukannya**.

Itu tidak otomatis. S2 membuktikan `tool.run_async()` langsung TIDAK memicu
callback apa pun - callback dipicu oleh flow di
`flows/llm_flows/functions.py:510-569`, bukan oleh tool. Jadi rantainya harus
direplikasi di sini, atau audit per-tool, deny-rule, HITL, dan hook redaksi
semuanya mati tanpa suara.
"""

from __future__ import annotations

import copy
import inspect
import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.tools.base_tool import BaseTool

from codemode.catalog import Catalog

logger = logging.getLogger(__name__)


def _error_text(result: dict[str, Any]) -> str:
    """Ambil pesan yang bisa dibaca dari envelope MCP yang gagal."""
    blocks = result.get("content")
    if isinstance(blocks, list):
        texts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        if texts:
            return " ".join(texts)[:1000]
    return str(result)[:1000]


class McpToolFailed(Exception):
    """Tool MCP melaporkan kegagalan lewat `isError: true`.

    Terukur: `McpTool.run_async` TIDAK melempar exception saat server MCP
    melaporkan kegagalan. Hasilnya kembali sebagai nilai normal dengan
    `isError: true` di dalamnya - misalnya membaca berkas yang tidak ada
    mengembalikan `{content:[{text:"ENOENT..."}], isError:true}`.

    Di direct-mode itu tidak berbahaya: model membaca teksnya dan paham.
    Di code mode itu fatal, karena kode sandbox membaca return value dan akan
    lanjut seolah berhasil. Jadi harus diubah jadi exception.
    """


class UnknownToolError(Exception):
    """Sandbox meminta tool yang tidak ada di katalog.

    Hanya terjadi kalau pesan protokol dipalsukan: objek `tools` di sandbox
    dibangun dari manifest, jadi nama yang salah gagal sebagai TypeError di
    sana, bukan sampai ke sini.
    """


def derive_tool_context(parent: Context, *, call_id: str) -> Context:
    """ToolContext turunan untuk satu tool call di dalam sandbox.

    Dua hal yang harus benar sekaligus:

    - `function_call_id` **berbeda** per call. Punya parent adalah milik
      `execute_code`; memakainya untuk tool dalam sandbox akan salah atribusi.
    - `event_actions` **dibagi** dengan parent. Kalau dibuat baru, state delta
      dan artifact delta dari tool dalam sandbox akan **hilang diam-diam**,
      karena flow hanya mempersistensi EventActions milik `execute_code`.
    """
    return Context(
        parent._invocation_context,
        event_actions=parent.actions,
        function_call_id=f"{parent.function_call_id or 'execute_code'}#{call_id}",
    )


class RpcBridge:
    """Menjembatani tool call sandbox ke ADK, lengkap dengan rantai callback."""

    def __init__(self, catalog: Catalog, tools: dict[tuple[str, str], BaseTool]):
        self._catalog = catalog
        self._tools = tools
        self.call_log: list[tuple[str, str]] = []

    @classmethod
    def from_pairs(
        cls, catalog: Catalog, pairs: dict[tuple[str, str], BaseTool]
    ) -> RpcBridge:
        return cls(catalog, pairs)

    def invoker(self, tool_context: Context):
        """Bangun callable yang cocok dengan `ToolInvoker` milik sandbox."""
        counter = {"n": 0}

        async def invoke(server: str, tool_name: str, args: dict[str, Any]) -> Any:
            counter["n"] += 1
            self.call_log.append((server, tool_name))
            return await self._run(
                server, tool_name, args, tool_context, str(counter["n"])
            )

        return invoke

    # ------------------------------------------------------------------ inti
    async def _run(
        self,
        server: str,
        tool_name: str,
        args: dict[str, Any],
        parent_ctx: Context,
        call_id: str,
    ) -> Any:
        tool = self._tools.get((server, tool_name))
        if tool is None:
            raise UnknownToolError(
                f"tool tidak dikenal: {server}.{tool_name}. "
                f"Tersedia: {sorted(n for _, n in self._tools)[:12]}"
            )

        ictx: InvocationContext = parent_ctx._invocation_context
        agent = ictx.agent
        pm = ictx.plugin_manager
        ctx = derive_tool_context(parent_ctx, call_id=call_id)

        # ADK men-deepcopy args sebelum callback (functions.py:482) karena
        # callback boleh memutasinya. Argumen dari sandbox juga diisolasi.
        args = copy.deepcopy(args)

        # 1. plugin before
        response = await pm.run_before_tool_callback(
            tool=tool, tool_args=args, tool_context=ctx
        )

        # 2. canonical before - break begitu ada hasil
        if response is None:
            for cb in agent.canonical_before_tool_callbacks:
                response = cb(tool=tool, args=args, tool_context=ctx)
                if inspect.isawaitable(response):
                    response = await response
                if response:
                    logger.debug(
                        "before_tool menimpa %s.%s - tool tidak dijalankan",
                        server,
                        tool_name,
                    )
                    break

        # 3. jalankan tool
        if response is None:
            try:
                response = await tool.run_async(args=args, tool_context=ctx)
            except Exception as err:
                handled = await self._handle_error(pm, agent, tool, args, ctx, err)
                if handled is None:
                    # WAJIB raise. ADK mengembalikan kegagalan tool sebagai
                    # nilai untuk dibaca model, tapi kode sandbox membaca
                    # return value - kalau diteruskan sebagai nilai, kode
                    # lanjut seolah berhasil.
                    raise
                response = handled

        # 4. plugin after
        altered = await pm.run_after_tool_callback(
            tool=tool, tool_args=args, tool_context=ctx, result=response
        )

        # 5. canonical after
        if altered is None:
            for cb in agent.canonical_after_tool_callbacks:
                altered = cb(
                    tool=tool, args=args, tool_context=ctx, tool_response=response
                )
                if inspect.isawaitable(altered):
                    altered = await altered
                if altered:
                    break

        final = altered if altered is not None else response

        # Dilakukan SETELAH seluruh rantai callback, bukan sebelum: di
        # direct-mode ADK memperlakukan hasil ber-isError sebagai hasil sukses,
        # jadi callback memang harus tetap melihatnya. Yang berbeda hanya apa
        # yang diterima sandbox. Callback redaksi pun masih bisa membersihkan
        # `isError` dan membuat call ini dianggap berhasil.
        if isinstance(final, dict) and final.get("isError"):
            raise McpToolFailed(f"{server}.{tool_name} gagal: {_error_text(final)}")

        return final

    @staticmethod
    async def _handle_error(pm, agent, tool, args, ctx, err: Exception):
        """URUTAN PENTING: plugin dulu, lalu canonical (functions.py:454-477)."""
        handled = await pm.run_on_tool_error_callback(
            tool=tool, tool_args=args, tool_context=ctx, error=err
        )
        if handled is not None:
            return handled
        for cb in agent.canonical_on_tool_error_callbacks:
            handled = cb(tool=tool, args=args, tool_context=ctx, error=err)
            if inspect.isawaitable(handled):
                handled = await handled
            if handled is not None:
                return handled
        return None
