"""Varian sandbox yang dikeraskan: Deno DI DALAM container.

Kenapa ada: flag permission Deno (`--deny-*`) ditegakkan oleh runtime yang
sama yang menjalankan kode model. Itu mencegah **kecelakaan**, bukan
**penyerang** - dan dokumen Deno sendiri menyatakannya bukan batas keamanan.
Satu bug di V8 atau di logika permission, dan tidak ada apa pun di bawahnya:
proses itu berjalan sebagai user Windows pemiliknya, dengan akses ke seluruh
disk dan ke `.env`.

Yang benar-benar dibeli lapisan ini - semuanya ditegakkan kernel, bukan
runtime:

| batas | mekanisme |
|---|---|
| memori | cgroup `--memory` (sebelumnya **tidak dibatasi sama sekali**) |
| CPU | cgroup `--cpus` |
| proses anak / fork bomb | `--pids-limit` |
| jaringan | `--network none` - tidak ada interface, bukan sekadar flag |
| filesystem | rootfs `--read-only` + satu `--tmpfs` kecil |
| privilege | `--cap-drop ALL`, `--security-opt no-new-privileges`, user non-root |

Yang lapisan ini TIDAK selesaikan, supaya tidak salah harap:

1. Sandbox tetap punya stdio ke host pemegang token - itu memang desainnya
   (supervisor memegang token, sesuai blog). Permukaan serangan sebenarnya
   adalah channel RPC, dan container tidak menyentuhnya.
2. Penyalahgunaan tool yang sah tidak dihalangi container. Itu urusan jalur
   `direct` (Fase 5a) dan rantai callback.
3. Eksfiltrasi lewat `console.log` tetap mungkin. Itu urusan `OutputGuard`.

Penyimpangan dari blog: Cloudflare memakai isolate V8 yang dikeraskan infra
mereka. Isolasi itu properti infra, bukan properti pustaka, jadi tidak bisa
direplikasi lokal. Container adalah padanan terdekat yang bisa dijalankan dan
**bisa diuji** di mesin pengembang.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from codemode.sandbox.deno import DENY_FLAGS, RUNTIME, DenoSandbox, Limits

#: Image resmi Deno. `alpine` dipilih karena paling kecil; `distroless` tidak
#: dipakai karena tidak punya shell untuk diagnosa saat gerbang gagal.
DEFAULT_IMAGE = "denoland/deno:alpine"


@dataclass
class Hardening:
    """Batas yang ditegakkan kernel, bukan runtime.

    Angkanya sengaja ketat: beban kerja yang dituju adalah menyaring hasil
    tool, bukan komputasi berat. Kalau sebuah tugas butuh lebih dari ini,
    yang salah adalah tugasnya.
    """

    image: str = DEFAULT_IMAGE
    memory_mb: int = 256
    #: Harus >= memory_mb pada Docker Desktop: swap dihitung TOTAL
    #: (memori + swap), jadi menyetelnya sama dengan memory_mb berarti
    #: swap nol - itu yang kita mau, supaya batas memori benar-benar mengikat.
    swap_mb: int | None = None
    cpus: float = 1.0
    pids: int = 64
    tmpfs_mb: int = 16
    #: Non-root. UID 1000 ada di image Deno resmi (`deno`).
    user: str = "1000:1000"
    #: Argumen docker tambahan, untuk eksperimen tanpa mengubah kode.
    extra_args: tuple[str, ...] = field(default_factory=tuple)


def find_docker() -> str:
    found = shutil.which("docker.exe") or shutil.which("docker")
    if found:
        return found
    raise FileNotFoundError(
        "docker tidak ditemukan. Pasang Docker Desktop, Podman, atau Rancher "
        "Desktop. Podman rootless adalah pengganti yang lebih kuat dan "
        "API-nya kompatibel - setel `docker_path` ke `podman`."
    )


class DockerSandbox(DenoSandbox):
    """`DenoSandbox` dengan proses dijalankan di dalam container.

    Hanya `_spawn()` yang berbeda. Protokol stdio, pump, OutputGuard, batas
    jumlah call, dan timeout semuanya diwarisi apa adanya - jadi tidak ada
    jalur kedua yang bisa menyimpang diam-diam dari jalur yang sudah diuji.
    """

    def __init__(
        self,
        *,
        limits: Limits | None = None,
        hardening: Hardening | None = None,
        docker_path: str | None = None,
        runtime: Path | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self.hardening = hardening or Hardening()
        self._docker = docker_path or find_docker()
        self._runtime = runtime or RUNTIME
        self._deno = "deno"  # entrypoint image, tidak dipakai dari host

    def docker_args(self) -> list[str]:
        """Argumen `docker run`. Dipisah agar bisa diperiksa test tanpa
        menjalankan container."""
        h = self.hardening
        swap = h.swap_mb if h.swap_mb is not None else h.memory_mb
        return [
            "run",
            "--rm",
            "-i",
            # --- isolasi
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-u",
            h.user,
            # --- resource
            "--memory",
            f"{h.memory_mb}m",
            "--memory-swap",
            f"{swap}m",
            "--cpus",
            str(h.cpus),
            "--pids-limit",
            str(h.pids),
            # --- filesystem: rootfs read-only, satu tmpfs kecil untuk DENO_DIR
            "--read-only",
            "--tmpfs",
            f"/tmp:size={h.tmpfs_mb}m,exec",
            "-e",
            "DENO_DIR=/tmp/deno",
            # runtime.ts di-mount read-only, bukan dicopy ke image: supaya
            # tidak ada langkah build yang bisa basi terhadap sumbernya.
            "-v",
            f"{self._runtime}:/runtime.ts:ro",
            *h.extra_args,
            h.image,
            "run",
            *DENY_FLAGS,
            "/runtime.ts",
        ]

    async def _spawn(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self._docker,
            *self.docker_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
