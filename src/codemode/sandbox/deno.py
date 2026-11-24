"""Sandbox Deno - spawn, protokol, batas.

Lapisan ini TIDAK tahu soal MCP maupun ADK. Tool call diteruskan ke sebuah
callable yang disuntikkan (`ToolInvoker`); yang menyambungkannya ke ADK adalah
`RpcBridge` di Fase 3.

Lifecycle: fresh per eksekusi, lalu dibuang. Setia blog ("create a new one for
every piece of code... throw it away"). Terukur ~85 ms end-to-end (S4), jadi
tidak ada pool yang perlu dibangun - dan tidak ada kebocoran state antar
snippet.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from codemode.protocol import (
    Call,
    Done,
    Failed,
    Init,
    Log,
    ProtocolError,
    Result,
    ToolErrorPayload,
    parse_inbound,
)
from codemode.sandbox.output_guard import OutputGuard

RUNTIME = Path(__file__).parent / "runtime.ts"

# Tanpa satu pun --allow-*. Deny eksplisit agar tidak bergantung pada default
# dan agar tidak ada prompt interaktif yang menggantung di sesi tanpa TTY.
#
# PENTING - `--deny-net` TIDAK CUKUP. Module loader Deno adalah subsistem
# terpisah dengan allow-list BAWAAN sendiri:
#     deno.land:443, jsr.io:443, esm.sh:443, raw.esm.sh:443,
#     cdn.jsdelivr.net:443, raw.githubusercontent.com:443,
#     gist.githubusercontent.com:443
# Dengan `--deny-net` saja, `await import("https://esm.sh/x")` tetap BERHASIL,
# dan `import "npm:lodash"` mengunduh dari registry.npmjs.org. Itu kanal
# eksfiltrasi: URL-nya sendiri sudah membocorkan data.
#
# `--deny-import` TIDAK memperbaikinya (tidak menimpa allow-list bawaan), dan
# `--cached-only` justru lebih buruk: modul yang sudah ter-cache tetap lolos.
# Yang benar: `--no-remote` + `--no-npm`. Keduanya tidak mengganggu `data:` URL
# yang kita pakai untuk memuat kode model.
DENY_FLAGS = (
    "--no-prompt",
    "--no-lock",
    "--no-remote",  # blokir https: dan jsr:
    "--no-npm",  # blokir npm:
    "--deny-net",
    "--deny-read",
    "--deny-write",
    "--deny-env",
    "--deny-ffi",
    "--deny-run",
    "--deny-sys",
)

Status = Literal[
    "done",
    "error",
    "timeout",
    "protocol_error",
    "crashed",
    # Gagal type-check SEBELUM dijalankan: nol tool call, nol efek samping.
    "type_error",
]

#: Diberi nama tool + argumen, kembalikan hasil atau lempar exception.
ToolInvoker = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


def find_deno() -> str:
    """Cari biner Deno, dan HINDARI shim `.cmd`/`.bat` npm.

    Urutannya bukan selera. Terukur di mesin Windows dengan Deno dipasang
    lewat npm: `shutil.which("deno")` mengembalikan `deno.CMD` (shim batch),
    dan spawn lewat shim itu **median 84 ms** versus **63 ms** lewat
    `deno.exe` asli - overhead 33% pada operasi yang justru harus murah,
    karena sandbox dibuat fresh tiap eksekusi.

    Jadi exe asli dicari lebih dulu, termasuk di lokasi npm yang biasanya
    TIDAK ada di PATH; shim hanya dipakai sebagai upaya terakhir.
    """
    if found := shutil.which("deno.exe"):
        return found

    guess = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/deno/deno.exe"
    if guess.exists():
        return str(guess)

    # Upaya terakhir: boleh jadi shim, tapi tetap jalan.
    if found := shutil.which("deno"):
        return found

    raise FileNotFoundError(
        "deno tidak ditemukan. Pasang dengan `npm i -g deno`. "
        "Catatan Windows: pakai deno.exe, bukan shim .cmd - shim tidak "
        "terlihat oleh CreateProcess dan melebih-lebihkan latensi spawn."
    )


@dataclass
class Limits:
    timeout_s: float = 60.0
    max_calls: int = 50
    max_total_chars: int = 100_000
    max_line_chars: int = 8_000
    max_lines: int = 2_000


@dataclass
class Execution:
    """Hasil satu eksekusi. `output` adalah yang dilihat model."""

    status: Status
    output: str
    calls: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    stack: str | None = None
    truncated: bool = False
    overflow: str | None = None
    stderr: str = ""
    #: JSON dari `export default` (kanal nilai kembali), belum ditimbang.
    value_json: str | None = None
    protocol_violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "done"


class DenoSandbox:
    def __init__(
        self,
        *,
        limits: Limits | None = None,
        deno_path: str | None = None,
        runtime: Path | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self._deno = deno_path or find_deno()
        self._runtime = runtime or RUNTIME

    async def run(
        self,
        code: str,
        *,
        manifest: dict[str, list[str]],
        flat: bool,
        invoke: ToolInvoker,
    ) -> Execution:
        t0 = time.perf_counter()
        guard = OutputGuard(
            max_total=self.limits.max_total_chars,
            max_line=self.limits.max_line_chars,
            max_lines=self.limits.max_lines,
        )
        state: dict[str, Any] = {
            "status": "crashed",
            "calls": 0,
            "error": None,
            "stack": None,
            "violations": [],
        }

        proc = await self._spawn()

        init = Init(
            flat=flat,
            manifest=manifest,
            max_calls=self.limits.max_calls,
            code=code,
        )

        try:
            await self._write(proc, init.model_dump_json())
            await asyncio.wait_for(
                self._pump(proc, guard, state, invoke),
                timeout=self.limits.timeout_s,
            )
        except TimeoutError:
            state["status"] = "timeout"
            state["error"] = (
                f"eksekusi melewati {self.limits.timeout_s}s dan dihentikan"
            )
        except ProtocolError as e:
            state["status"] = "protocol_error"
            state["error"] = str(e)
        finally:
            stderr = await self._terminate(proc)

        # Kalau proses mati tanpa mengirim done/error, pesan "crashed" saja
        # tidak memberi petunjuk apa pun - padahal penyebab paling sering
        # punya tanda tangan yang jelas. 137 = SIGKILL, dan di varian container
        # itu praktis selalu OOM killer cgroup.
        if state["status"] == "crashed" and state["error"] is None:
            rc = proc.returncode
            state["error"] = (
                "sandbox dihentikan paksa (exit 137) - hampir selalu batas "
                "memori container terlampaui; perkecil data yang ditahan di "
                "memori atau naikkan Hardening.memory_mb"
                if rc == 137
                else f"sandbox keluar tanpa mengirim done/error (exit {rc})"
            )

        return Execution(
            status=state["status"],
            output=guard.render(),
            calls=state["calls"],
            duration_ms=(time.perf_counter() - t0) * 1000,
            error=state["error"],
            stack=state["stack"],
            truncated=guard.truncated,
            overflow=guard.overflow_text(),
            stderr=stderr,
            value_json=state.get("value_json"),
            protocol_violations=state["violations"],
        )

    async def _spawn(self) -> asyncio.subprocess.Process:
        """Satu-satunya titik yang menentukan BAGAIMANA runtime dijalankan.

        Dipisah supaya varian yang dikeraskan (`DockerSandbox`) hanya perlu
        menimpa ini - seluruh protokol, pump, dan penegakan batas di host tetap
        sama, jadi tidak ada jalur kedua yang bisa menyimpang diam-diam.
        """
        return await asyncio.create_subprocess_exec(
            self._deno,
            "run",
            *DENY_FLAGS,
            str(self._runtime),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # ------------------------------------------------------------- internal
    @staticmethod
    async def _write(proc: asyncio.subprocess.Process, payload: str) -> None:
        assert proc.stdin is not None
        proc.stdin.write((payload + "\n").encode())
        await proc.stdin.drain()

    async def _pump(
        self,
        proc: asyncio.subprocess.Process,
        guard: OutputGuard,
        state: dict[str, Any],
        invoke: ToolInvoker,
    ) -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                # Pesan error-nya diisi di `run()` setelah proses dipanen,
                # supaya exit code ikut dilaporkan (137 = dibunuh OOM killer).
                return

            try:
                msg = parse_inbound(raw.decode("utf-8", errors="replace"))
            except ProtocolError as e:
                # Jangan matikan eksekusi karena satu baris rusak; catat saja.
                # Baris rusak TIDAK pernah ditafsirkan sebagai perintah.
                state["violations"].append(str(e)[:300])
                if len(state["violations"]) > 20:
                    raise
                continue

            if isinstance(msg, Log):
                guard.add(msg.data, stream=msg.stream)
            elif isinstance(msg, Done):
                state["status"] = "done"
                state["value_json"] = msg.value_json
                return
            elif isinstance(msg, Failed):
                state["status"] = "error"
                state["error"] = msg.message
                state["stack"] = msg.stack
                return
            elif isinstance(msg, Call):
                await self._handle_call(proc, msg, state, invoke)

    async def _handle_call(
        self,
        proc: asyncio.subprocess.Process,
        msg: Call,
        state: dict[str, Any],
        invoke: ToolInvoker,
    ) -> None:
        # Batas ditegakkan di host juga, bukan hanya di sandbox: sandbox
        # menjalankan kode model dan tidak boleh jadi penjaga batasnya sendiri.
        if state["calls"] >= self.limits.max_calls:
            await self._write(
                proc,
                Result(
                    id=msg.id,
                    ok=False,
                    error=ToolErrorPayload(
                        type="CallLimitError",
                        message=f"batas {self.limits.max_calls} tool call "
                        "per eksekusi terlampaui",
                        server=msg.server,
                        tool=msg.tool,
                    ),
                ).model_dump_json(),
            )
            return

        state["calls"] += 1
        try:
            value = await invoke(msg.server, msg.tool, msg.args)
            payload = Result(id=msg.id, ok=True, value=value)
        except Exception as e:
            # WAJIB jadi error, bukan nilai. ADK mengembalikan kegagalan tool
            # sebagai nilai, tapi kode sandbox membaca return value - kalau
            # diteruskan sebagai nilai, kode lanjut seolah berhasil.
            payload = Result(
                id=msg.id,
                ok=False,
                error=ToolErrorPayload(
                    type=type(e).__name__,
                    message=str(e)[:2000],
                    server=msg.server,
                    tool=msg.tool,
                    status=getattr(e, "status_code", None),
                    body=_as_text(getattr(e, "body", None)),
                ),
            )
        try:
            await self._write(proc, payload.model_dump_json())
        except (BrokenPipeError, ConnectionResetError):
            return  # sandbox sudah mati; _pump akan berhenti sendiri

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> str:
        if proc.stdin is not None and not proc.stdin.is_closing():
            with contextlib.suppress(
                BrokenPipeError, ConnectionResetError, RuntimeError
            ):
                proc.stdin.close()
        if proc.returncode is None:
            proc.kill()
        stderr = b""
        if proc.stderr is not None:
            with contextlib.suppress(TimeoutError):
                stderr = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        # Tutup transport eksplisit: tanpa ini asyncio di Windows memunculkan
        # ResourceWarning "unclosed transport" saat GC.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        return stderr.decode("utf-8", errors="replace").strip()[:2000]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)[:2000]


def manifest_from_catalog(catalog: Any) -> tuple[dict[str, list[str]], bool]:
    """Katalog -> (manifest, flat). Bentuknya harus cocok dengan apa yang
    dibangkitkan Codegen, atau kode model akan memanggil nama yang tidak ada."""
    manifest = {
        server: [t.name for t in specs] for server, specs in catalog.by_server().items()
    }
    return manifest, catalog.is_single_server
