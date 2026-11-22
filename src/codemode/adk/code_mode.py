"""CodeMode - fasad in-process, meniru `codemode()` Cloudflare.

    const { system, tools } = codemode({ system, tools, ...config })

Padanannya di sini:

    cm = CodeMode({"fs": McpToolset(...)})
    agent = LlmAgent(
        name="...", model="...",
        instruction=cm.instruction_provider("Kamu asisten yang membantu."),
        tools=[cm.tool],
    )

Supervisor = agent = pemegang token, sesuai blog: "All calls made on it go to
the agent supervisor first, which holds the access tokens."
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from google.adk.agents.context import Context
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.genai import types

from codemode.catalog import (
    Catalog,
    DirectPolicy,
    DirectSelector,
    build_catalog,
    enable_json_schema_feature,
)
from codemode.codegen import RENDERERS
from codemode.sandbox import DenoSandbox, Execution, Limits

Source = BaseToolset | Sequence[BaseTool]

PROMPT = """\
# Mode eksekusi kode

Kamu TIDAK memanggil tool satu per satu. Kamu menulis **TypeScript** dan
menjalankannya dengan satu tool: `execute_code`.

Di dalam kode itu, `tools` sudah tersedia sebagai objek global. Deklarasi
lengkapnya ada di bawah.

Aturan:

1. **Hanya `console.log` yang kembali kepadamu.** Nilai kembalian skrip
   diabaikan. Yang tidak kamu log, tidak akan kamu lihat.
2. **Filter di dalam kode, jangan me-log semuanya.** Inilah gunanya mode ini:
   ambil data besar, saring, lalu log hanya yang kamu butuh. Me-log 10.000
   baris membuang keunggulannya.
3. **Tidak ada jaringan dan tidak ada `import`.** `fetch`, `Deno.*`, dan import
   remote semuanya diblokir. Satu-satunya jalan keluar adalah `tools`.
4. **Rangkai beberapa call dalam satu eksekusi.** Pakai hasil call pertama
   sebagai masukan call berikutnya; pakai `Promise.all` untuk call yang
   independen.
5. **Kegagalan tool dilempar sebagai exception.** Pakai `try/catch` kalau kamu
   ingin menanganinya; kalau tidak, skrip berhenti.
6. TypeScript berjalan native - `interface`, generic, dan `await` di level atas
   semuanya boleh.

## API yang tersedia

```typescript
{declarations}
```
{direct_block}"""

#: Blok tambahan saat ada tool jalur `direct` (Fase 5). Tool destruktif tidak
#: boleh dipanggil dari dalam sandbox: persetujuan pengguna, kredensial, dan
#: jejak audit per-call adalah urusan agent loop, bukan urusan kode model.
DIRECT_PROMPT = """
## Tool yang dipanggil langsung

Tool berikut **tidak ada** di dalam objek `tools` dan tidak bisa dipanggil
dari kode. Panggil sebagai tool biasa, satu per satu:

{listing}

Polanya: pakai `execute_code` untuk membaca dan menyaring, lalu panggil tool
di atas untuk tindakan yang mengubah sesuatu - dengan argumen yang sudah kamu
pastikan dari hasil pembacaan itu.
"""


class CodeMode:
    """Bungkus sekumpulan tool jadi satu tool eksekusi kode."""

    def __init__(
        self,
        sources: dict[str, Source],
        *,
        limits: Limits | None = None,
        language: str = "typescript",
        flat: bool | None = None,
        sandbox: DenoSandbox | None = None,
        direct: DirectSelector | None = None,
        direct_policy: DirectPolicy = DirectPolicy.OFF,
    ) -> None:
        if language not in RENDERERS:
            raise ValueError(
                f"bahasa {language!r} tidak dikenal. Pilihan: {sorted(RENDERERS)}"
            )
        enable_json_schema_feature()
        self._sources = sources
        self._language = language
        self._flat_override = flat
        self._direct = direct
        self._direct_policy = direct_policy
        self._limits = limits or Limits()
        self._sandbox = sandbox or DenoSandbox(limits=self._limits)

        self._direct_tools: list[BaseTool] = []
        self._catalog: Catalog | None = None
        self._declarations: str | None = None
        self._tool_map: dict[tuple[str, str], BaseTool] = {}
        self._lock = asyncio.Lock()
        self.executions: list[Execution] = []
        #: (kode, status, error) tiap eksekusi - untuk diagnosa kegagalan.
        #: Tanpa ini, perbaikan prompt hanya tebakan.
        self.trace: list[tuple[str, str, str | None]] = []

    # ----------------------------------------------------------------- setup
    async def prepare(self) -> Catalog:
        """Bangun katalog sekali, lalu pakai ulang.

        Dipanggil dari dua jalur asinkron (instruction provider dan eksekusi
        tool), jadi dikunci.
        """
        if self._catalog is not None:
            return self._catalog
        async with self._lock:
            if self._catalog is not None:
                return self._catalog
            catalog = await build_catalog(
                self._sources,
                direct=self._direct,
                direct_policy=self._direct_policy,
            )
            # Peta untuk RpcBridge sengaja dibangun dari `catalog.tools`,
            # BUKAN dari seluruh sumber. Kalau diisi semua tool, pesan
            # protokol yang dipalsukan dari sandbox bisa menjangkau tool
            # direct - persis yang jalur ini ingin cegah.
            allowed = {(s.server, s.name) for s in catalog.tools}
            tool_map: dict[tuple[str, str], BaseTool] = {}
            for server, source in self._sources.items():
                tools = (
                    await source.get_tools()
                    if isinstance(source, BaseToolset)
                    else list(source)
                )
                for t in tools:
                    if (server, t.name) in allowed:
                        tool_map[(server, t.name)] = t
            self._tool_map = tool_map
            self._direct_tools = [d.tool for d in catalog.direct if d.tool]
            self._declarations = RENDERERS[self._language](
                catalog, flat=self._flat_override
            )
            self._catalog = catalog
            return catalog

    @property
    def catalog(self) -> Catalog:
        if self._catalog is None:
            raise RuntimeError("panggil `await prepare()` dulu")
        return self._catalog

    @property
    def declarations(self) -> str:
        if self._declarations is None:
            raise RuntimeError("panggil `await prepare()` dulu")
        return self._declarations

    # ----------------------------------------------------------- instruction
    async def instruction_block(self) -> str:
        catalog = await self.prepare()
        return PROMPT.format(
            declarations=self.declarations,
            direct_block=_direct_block(catalog),
        )

    def instruction_provider(
        self, base: str = ""
    ) -> Callable[[ReadonlyContext], Awaitable[str]]:
        """Provider async untuk `LlmAgent(instruction=...)`.

        S1 membuktikan ADK meng-`await` provider. Catatan: dengan provider,
        `bypass_state_injection=True`, jadi `{var}` TIDAK di-inject otomatis -
        blok ini statis, jadi tidak masalah.
        """

        async def provide(_ctx: ReadonlyContext) -> str:
            block = await self.instruction_block()
            return f"{base}\n\n{block}" if base else block

        return provide

    # ------------------------------------------------------------------ tool
    @property
    def tool(self) -> BaseTool:
        from codemode.adk.execute_tool import ExecuteCodeTool

        return ExecuteCodeTool(self)

    @property
    def direct_toolset(self) -> BaseToolset:
        """Tool jalur `direct` sebagai toolset.

        Toolset, bukan list, karena isinya baru diketahui setelah `prepare()`
        yang asinkron - sementara `LlmAgent(tools=[...])` dibangun sinkron.
        ADK memanggil `get_tools()` saat menyusun request, jadi urutannya aman.
        """
        return _DirectToolset(self)

    @property
    def tools(self) -> list[BaseTool | BaseToolset]:
        """Isi `LlmAgent(tools=...)`: satu tool eksekusi kode + jalur direct."""
        return [self.tool, self.direct_toolset]

    async def direct_tools(self) -> list[BaseTool]:
        await self.prepare()
        return list(self._direct_tools)

    # ------------------------------------------------------------- eksekusi
    async def execute(self, code: str, tool_context: Context) -> Execution:
        from codemode.adk.bridge import RpcBridge

        catalog = await self.prepare()
        bridge = RpcBridge(catalog, self._tool_map)
        manifest = {
            server: [t.name for t in specs]
            for server, specs in catalog.by_server().items()
        }
        flat = (
            catalog.is_single_server
            if self._flat_override is None
            else self._flat_override
        )
        result = await self._sandbox.run(
            code,
            manifest=manifest,
            flat=flat,
            invoke=bridge.invoker(tool_context),
        )
        self.executions.append(result)
        self.trace.append((code, result.status, result.error))
        return result

    # ------------------------------------------------------------- diagnostik
    def report(self) -> dict[str, Any]:
        cat = self._catalog
        return {
            "tools": len(cat.tools) if cat else 0,
            "direct": [
                {"tool": d.qualified, "reason": d.reason}
                for d in (cat.direct if cat else [])
            ],
            "servers": cat.servers if cat else [],
            "declaration_chars": len(self._declarations or ""),
            "executions": len(self.executions),
        }

    async def close(self) -> None:
        for source in self._sources.values():
            if isinstance(source, BaseToolset):
                await source.close()


class _DirectToolset(BaseToolset):
    """Pembungkus tipis supaya tool direct ikut terdeklarasi ke model."""

    def __init__(self, code_mode: CodeMode) -> None:
        super().__init__()
        self._cm = code_mode

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        return await self._cm.direct_tools()

    async def close(self) -> None:
        # Siklus hidup toolset asli dipegang `CodeMode.close()`; menutup di
        # sini akan menutupnya dua kali.
        return None


def _direct_block(catalog: Catalog) -> str:
    if not catalog.direct:
        return ""
    listing = "\n".join(f"- `{d.name}` - {d.reason}" for d in catalog.direct)
    return DIRECT_PROMPT.format(listing=listing)


def code_tool_declaration() -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name="execute_code",
        description=(
            "Jalankan TypeScript di sandbox terisolasi untuk memanggil tool. "
            "Objek global `tools` tersedia (deklarasinya ada di instruction). "
            "Hanya keluaran console.log yang dikembalikan kepadamu."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "code": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Kode TypeScript. Pakai `await tools....()` untuk "
                        "memanggil tool dan `console.log()` untuk mengembalikan "
                        "hasil. Saring data di dalam kode."
                    ),
                )
            },
            required=["code"],
        ),
    )
