"""Gerbang keluar Fase 5b - container hardening, tanpa LLM.

Pertanyaan yang dijawab gerbang ini: **apakah container benar-benar menambah
batas, atau cuma menduplikasi apa yang sudah dilakukan flag Deno?**

Cara menjawabnya: tiap pemeriksaan **sengaja melonggarkan flag Deno** yang
relevan, lalu menuntut operasinya tetap gagal. Kalau ia berhasil, berarti satu
satunya penjaga memang runtime - dan container tidak membeli apa pun.

Empat pemeriksaan, semuanya bisa gagal:

1. `--allow-net` diberikan, `fetch` harus tetap gagal (`--network none`:
   tidak ada interface, bukan sekadar permission).
2. `--allow-write` diberikan, tulis ke rootfs harus gagal (`--read-only`),
   sementara tulis ke `/tmp` berhasil (tmpfs - jadi kegagalan di atas bukan
   karena Deno masih memblokir semuanya).
3. `--allow-sys` diberikan, UID harus non-root.
4. Batas memori cgroup mengikat: alokasi jauh di atas `--memory` harus
   mematikan proses, bukan menggerogoti RAM host. Inilah lubang yang paling
   nyata sebelum fase ini - tidak ada batas memori sama sekali.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from codemode.sandbox import DenoSandbox, DockerSandbox, Hardening, Limits
from codemode.sandbox.deno import DENY_FLAGS


class _Longgar(DockerSandbox):
    """Varian gerbang: satu flag deny ditukar jadi allow.

    Hanya dipakai di gerbang ini. Tujuannya membuktikan batas kernel berdiri
    sendiri - bukan untuk dipakai di produksi.
    """

    def __init__(self, *, tukar: dict[str, str], **kw) -> None:
        super().__init__(**kw)
        self._tukar = tukar

    def docker_args(self) -> list[str]:
        args = super().docker_args()
        return [self._tukar.get(a, a) for a in args]


async def _invoke(server: str, tool: str, args: dict) -> dict:
    return {"ok": True}


MANIFEST = {"fs": ["baca"]}


async def _run(sb: DockerSandbox, code: str):
    return await sb.run(code, manifest=MANIFEST, flat=True, invoke=_invoke)


async def main() -> int:
    failures: list[str] = []
    limits = Limits(timeout_s=120)

    # --- 0. jalur normal masih utuh
    sb = DockerSandbox(limits=limits)
    base = await _run(sb, 'console.log("hidup:", Deno.build.os);')
    print(f"jalur normal   : status={base.status} out={base.output.strip()!r}")
    if base.status != "done" or "linux" not in base.output:
        failures.append("container tidak bisa menjalankan jalur normal")

    # --- 1. jaringan: permission dilonggarkan, interface tetap tidak ada
    net = await _run(
        _Longgar(tukar={"--deny-net": "--allow-net"}, limits=limits),
        """
try {
  const r = await fetch("https://example.com", { signal: AbortSignal.timeout(8000) });
  console.log("BOCOR status", r.status);
} catch (e) {
  console.log("gagal:", (e as Error).name, "|", (e as Error).message.slice(0, 60));
}
""",
    )
    print(f"--allow-net    : {net.output.strip()!r}")
    if "BOCOR" in net.output:
        failures.append(
            "fetch BERHASIL dengan --allow-net: --network none tidak mengikat"
        )
    if net.status != "done":
        failures.append(f"pemeriksaan jaringan tidak selesai: {net.status} {net.error}")

    # --- 2. filesystem: rootfs read-only vs tmpfs
    fs = await _run(
        _Longgar(tukar={"--deny-write": "--allow-write"}, limits=limits),
        """
for (const p of ["/etc/disusupi", "/runtime_baru.ts", "/tmp/boleh.txt"]) {
  try {
    await Deno.writeTextFile(p, "x");
    console.log("TERTULIS", p);
  } catch (e) {
    console.log("ditolak", p, (e as Error).name);
  }
}
""",
    )
    print(f"--allow-write  : {fs.output.strip()!r}")
    bocor = [
        line
        for line in fs.output.splitlines()
        if line.startswith("TERTULIS") and "/tmp/" not in line
    ]
    if bocor:
        failures.append(f"rootfs bisa ditulis walau --read-only: {bocor}")
    if "TERTULIS /tmp/boleh.txt" not in fs.output:
        # Kalau /tmp juga gagal, pemeriksaan di atas tidak membuktikan apa pun:
        # bisa jadi Deno yang masih memblokir semuanya.
        failures.append(
            "tulis ke /tmp gagal - pemeriksaan read-only jadi tidak bermakna"
        )

    # --- 3. non-root
    uid = await _run(
        _Longgar(tukar={"--deny-sys": "--allow-sys"}, limits=limits),
        'console.log("uid:", Deno.uid(), "gid:", Deno.gid());',
    )
    print(f"--allow-sys    : {uid.output.strip()!r}")
    if "uid: 0 " in uid.output:
        failures.append("proses berjalan sebagai root di dalam container")
    if "uid:" not in uid.output:
        failures.append(f"UID tidak terbaca: {uid.status} {uid.error}")

    # --- 4. batas memori cgroup
    ketat = DockerSandbox(limits=limits, hardening=Hardening(memory_mb=64, tmpfs_mb=8))
    mem = await _run(
        ketat,
        """
const bongkahan: Uint8Array[] = [];
for (let i = 0; i < 4000; i++) {
  bongkahan.push(new Uint8Array(1024 * 1024).fill(i % 255));
  if (i % 200 === 0) console.log("MiB:", i);
}
console.log("TIDAK DIBATASI", bongkahan.length);
""",
    )
    print(f"memori 64m     : status={mem.status} err={(mem.error or '')[:90]}")
    print(f"  stderr       : {(mem.stderr or '').strip()[:120]!r}")
    if "TIDAK DIBATASI" in mem.output:
        failures.append(
            "alokasi 4 GiB SELESAI di container 64m - cgroup tidak mengikat"
        )
    if mem.status == "done":
        failures.append(f"status 'done' padahal seharusnya mati: {mem.output[-80:]}")

    # --- biaya: diukur, bukan ditebak
    print("\n--- biaya latensi (5 run, kode trivial)")
    for nama, s in [("lokal", DenoSandbox(limits=limits)), ("docker", sb)]:
        ms = []
        for _ in range(5):
            r = await s.run(
                'console.log("x");', manifest=MANIFEST, flat=True, invoke=_invoke
            )
            ms.append(r.duration_ms)
        print(f"  {nama:7} median={statistics.median(ms):.0f}ms")

    print()
    if failures:
        print("GERBANG FASE 5b: GAGAL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GERBANG FASE 5b: LOLOS")
    print(f"  (flag deny yang tetap aktif di jalur normal: {len(DENY_FLAGS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
