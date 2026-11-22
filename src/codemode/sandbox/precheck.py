"""Type-check kode model SEBELUM dijalankan.

Alasannya datang dari hasil Fase 4, bukan dari preferensi. Dua syarat yang
menentukan apakah klaim blog bertahan, dan yang pertama adalah: **kode harus
jadi di percobaan pertama**. `gemini-3.5-flash` hanya 8/16 (50%), dan di sana
code mode kalah 11/16; `cc/claude-sonnet-5-5` 24/24 dan menang telak. Jadi
tingkat jadi-sekali bukan detail - itu penentu.

Yang mahal dari kegagalan bukan eksekusinya, tapi **perbaikannya**: satu
putaran LLM penuh, lengkap dengan deklarasi API yang dikirim ulang. Ribuan
token dan beberapa detik. Dibandingkan itu, `deno check` berharga puluhan
hingga beberapa ratus milidetik - dan ia menangkap persis kelas kesalahan yang
paling sering: nama tool salah, argumen wajib hilang, tipe tidak cocok,
properti hasil yang tidak ada.

Bonus yang lebih penting dari kecepatan: kalau kode tidak lolos type-check,
**tidak ada tool call yang terjadi sama sekali**. Model yang salah menulis kode
tidak lagi bisa menjalankan separuh rantai lalu gagal di tengah.

Catatan pengukuran: `deno check` pertama mahal (~2,3 s, mengisi cache), lalu
median **~90 ms** untuk kode yang valid dan ~800 ms untuk kode yang punya
error (ongkos memformat diagnostik). Karena itu checker-nya menyimpan satu
direktori kerja per instance dan memakai ulang nama berkas - supaya cache Deno
tetap panas.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from codemode.sandbox.deno import find_deno

#: Kode warna ANSI dari diagnostik Deno - tidak berguna di konteks model.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Baris diagnostik yang membawa informasi. Sisanya (banner "Check file:///...",
#: baris kosong, penunjuk kolom) hanya menghabiskan konteks.
_PENTING = re.compile(r"^(TS\d+|error:|\s+at\s|.*\.ts:\d+:\d+)")

REFERENSI = '/// <reference path="./tools.d.ts" />\n'


@dataclass
class CheckResult:
    ok: bool
    #: Diagnostik yang sudah dipangkas, siap dikirim ke model.
    errors: str = ""
    duration_ms: float = 0.0
    #: False kalau pemeriksaan dilewati (deno tidak ada / timeout). Dilewati
    #: TIDAK pernah berarti gagal: precheck adalah optimasi, bukan gerbang
    #: keamanan. Batas keamanan ada di sandbox dan di host.
    checked: bool = True


class TypeChecker:
    """Pemeriksa tipe sekali pakai-berulang untuk satu set deklarasi.

    Satu instance per `CodeMode`: deklarasi ditulis sekali, lalu tiap
    pemeriksaan hanya menulis ulang satu berkas snippet.
    """

    def __init__(
        self,
        declarations: str,
        *,
        deno_path: str | None = None,
        max_errors: int = 6,
        max_chars: int = 2000,
        timeout_s: float = 30.0,
    ) -> None:
        self._deno = deno_path or find_deno()
        self._max_errors = max_errors
        self._max_chars = max_chars
        self._timeout_s = timeout_s
        self._lock = asyncio.Lock()
        self._dir = Path(tempfile.mkdtemp(prefix="codemode_check_"))
        (self._dir / "tools.d.ts").write_text(declarations, encoding="utf-8")
        self._snippet = self._dir / "snippet.ts"

    async def check(self, code: str) -> CheckResult:
        t0 = time.perf_counter()
        # Nama berkas dipakai ulang supaya cache Deno tetap panas; karena itu
        # pemeriksaan harus berurutan.
        async with self._lock:
            self._snippet.write_text(REFERENSI + code, encoding="utf-8")
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._deno,
                    "check",
                    "--no-lock",
                    "--no-remote",
                    "--no-npm",
                    self._snippet.name,
                    cwd=self._dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:  # deno hilang di tengah jalan
                return CheckResult(
                    ok=True, checked=False, errors=f"precheck dilewati: {e}"
                )

            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_s
                )
            except TimeoutError:
                proc.kill()
                return CheckResult(
                    ok=True,
                    checked=False,
                    errors="precheck dilewati: deno check melewati batas waktu",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

        ms = (time.perf_counter() - t0) * 1000
        if proc.returncode == 0:
            return CheckResult(ok=True, duration_ms=ms)

        teks = (err or b"").decode("utf-8", errors="replace") or (
            (out or b"").decode("utf-8", errors="replace")
        )
        return CheckResult(ok=False, errors=self._pangkas(teks), duration_ms=ms)

    def _pangkas(self, teks: str) -> str:
        bersih = _ANSI.sub("", teks)
        # Path temp dir tidak berguna bagi model dan membocorkan nama mesin.
        bersih = bersih.replace(str(self._dir).replace("\\", "/"), "").replace(
            str(self._dir), ""
        )
        baris = [b.rstrip() for b in bersih.splitlines() if b.strip()]
        penting = [b for b in baris if _PENTING.match(b.strip()) or "TS" in b]
        dipakai = (penting or baris)[: self._max_errors * 3]
        hasil = "\n".join(dipakai)[: self._max_chars]
        return hasil or "deno check gagal tanpa diagnostik"

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
