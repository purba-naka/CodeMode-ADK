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

from codemode.catalog import Catalog, build_catalog, enable_json_schema_feature
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
    ) -> None:
        if language not in RENDERERS:
            raise ValueError(
                f"bahasa {language!r} tidak dikenal. Pilihan: {sorted(RENDERERS)}"
            )
        enable_json_schema_feature()
        self._sources = sources
        self._language = language
        self._flat_override = flat
        self._limits = limits or Limits()
        self._sandbox = sandbox or DenoSandbox(limits=self._limits)

        self._catalog: Catalog | None = None
        self._declarations: str | None = None
        self._tool_map: dict[tuple[str, str], BaseTool] = {}
        self._lock = asyncio.Lock()
        self.executions: list[Execution] = []

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
            catalog = await build_catalog(self._sources)
            tool_map: dict[tuple[str, str], BaseTool] = {}
            for server, source in self._sources.items():
                tools = (
                    await source.get_tools()
                    if isinstance(source, BaseToolset)
                    else list(source)
                )
                for t in tools:
                    tool_map[(server, t.name)] = t
            self._tool_map = tool_map
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
        await self.prepare()
        return PROMPT.format(declarations=self.declarations)

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
        return result

    # ------------------------------------------------------------- diagnostik
    def report(self) -> dict[str, Any]:
        cat = self._catalog
        return {
            "tools": len(cat.tools) if cat else 0,
            "rejected": [
                {"tool": r.name, "reason": r.reason}
                for r in (cat.rejected if cat else [])
            ],
            "servers": cat.servers if cat else [],
            "declaration_chars": len(self._declarations or ""),
            "executions": len(self.executions),
        }

    async def close(self) -> None:
        for source in self._sources.values():
            if isinstance(source, BaseToolset):
                await source.close()


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
