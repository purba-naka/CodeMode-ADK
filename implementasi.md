# Code Mode untuk Google ADK — Rencana Implementasi

Bridge yang menerapkan pola **Code Mode** (Cloudflare) / **Code execution with MCP**
(Anthropic) ke Google ADK: alih-alih mengekspos MCP tool satu per satu ke LLM,
tool diubah menjadi API berbahasa pemrograman, dan model menulis kode yang
memanggil API itu di dalam sandbox.

Referensi:
- https://blog.cloudflare.com/code-mode/
- https://www.anthropic.com/research/code-execution-with-mcp
- https://www.anthropic.com/research/advanced-tool-use (Programmatic Tool Calling)

---

## 1. Pendekatan: setia pada blog Cloudflare

Varian yang dipilih adalah **in-process, `codemode()`-style** — library yang
dibungkus di sekitar agent, bukan servis terpisah.

Alasan: di blog Cloudflare, **agent loop = supervisor = MCP client = pemegang
token**. Satu entitas.

> "These APIs are backed by RPC invocation which calls back to *the agent loop*.
> There, the Agents SDK dispatches the call to the appropriate MCP server."

> "All calls made on it go to *the agent supervisor* first, *which holds the
> access tokens* and adds them into requests sent on to MCP."

Memisahkan supervisor dari agent (varian "gateway") memberi keuntungan agnostik
framework, tapi meruntuhkan identitas kredensial menjadi satu identitas bersama
dan menghilangkan callback/HITL/state ADK. Ditunda — lihat §8.

---

## 2. Keputusan yang dikunci

| Aspek | Pilihan | Dasar |
|---|---|---|
| Topologi | Library in-process, diimpor `agent.py` | Setia `codemode()` Cloudflare |
| MCP client | `McpToolset` ADK | Sudah punya auth/OAuth/reconnect/transport |
| Pemegang kredensial | Proses ADK | Blog: "the agent supervisor holds the access tokens" |
| Disclosure | Seluruh API upfront | Blog: "Currently, the entire API is loaded" |
| Tool ke model | Satu tool eksekusi kode | Blog: "presented with just one tool" |
| Egress sandbox | stdio ke parent, tanpa jaringan | Semangat bindings: kapabilitas, bukan jaringan |
| Jalur hasil | **hanya `console.log`** | Blog: "returns results... by invoking `console.log()`. When the script finishes, all the output logs are passed back". **Tidak ada nilai kembalian** — lihat §9 |
| Lifecycle sandbox | **Fresh per `execute_code` call** — final | Blog: "create a new one for every piece of code... There's no need to reuse them... run the code, and throw it away". **Dikonfirmasi S4:** end-to-end median 85 ms (ambang 150 ms). Tidak ada pool |
| Eksekusi string TS | **`data:application/typescript` + `import()`** | Dikonfirmasi S4: Deno mentranspile TS dari data URL dengan **semua permission ditolak**. Tanpa esbuild/swc, tanpa file temporer, tanpa sourcemap |
| Sumber katalog | **`BaseTool` ADK apa pun**, `McpTool` sebagai kasus kaya | Blog membungkus tool generik, "MCP servers included". Sekaligus jalan keluar risiko S3 |
| Registrasi tool | `FunctionTool`, **bukan** `code_executor=` | Menghindari batasan one-tool-per-agent + `MALFORMED_FUNCTION_CALL` |
| Bahasa sandbox | **TypeScript** | Setia blog. Pilihan bahasa adalah variabel yang **diukur** (Fase 4), bukan diasumsikan |
| Runtime sandbox (L1) | **Deno** | TS native tanpa transpiler, `--deny-*` deklaratif, satu biner, jalan di Windows |
| Transport RPC | stdio, JSON Lines bertipe | Tanpa jaringan sama sekali -> `--deny-net` benar-benar total |
| Codegen | **Pluggable** | Akan ada dua implementasi (TS, Python). Bukan abstraksi spekulatif |
| Server uji (realisme) | `npx @modelcontextprotocol/server-filesystem` | stdio, hasil mudah diverifikasi — untuk task rantai & data-heavy |
| Server uji (penskalaan) | **mock berparameter** | `server-filesystem` hanya ~11 tool berschema sederhana → **tidak bisa** menguji klaim "many more tools" & "more complex tools". Lihat Fase 4 |

### Tidak dipakai

- **workerd / Worker Loader.** `workerd` lokal **bukan** hardened sandbox
  menurut dokumen Cloudflare sendiri ("not a hardened sandbox. Only run code you
  trust"), dan Kenton Varda menyatakan "the production runtime would need a very
  different implementation". Isolasi kuat Code Mode adalah properti infra
  Cloudflare, bukan properti workerd yang di-self-host. Juga tidak ada build
  step — TS harus ditranspile dulu.
- **Rust sebagai bahasa yang ditulis model.** Kompilasi butuh detik, bukan
  milidetik — ekonomi code mode runtuh untuk kode sekali pakai. Borrow checker
  adalah kelas error yang paling sulit diperbaiki LLM tanpa REPL. Cargo butuh
  jaringan, yang persisnya kita matikan. Rust unggul untuk program yang
  dijalankan ribuan kali; kode code mode dijalankan sekali lalu dibuang.
  Catatan: Rust tetap dipakai secara tidak langsung — Deno, wasmtime, libkrun,
  Firecracker, Monty semuanya ditulis dengan Rust.
- **Sandbox cloud** (E2B/Modal/Daytona cloud). Sandbox di cloud harus menelepon
  balik host → butuh tunnel dan membalik model keamanan. Terukur ~1,5 s create.
- **Daytona self-hosted.** Repo diarsipkan Okt 2026, pengembangan pindah ke
  closed source Juni 2026. Tidak layak untuk komponen yang tugasnya mengurung
  kode bermusuhan.

### Ditunda ke v1

Jalur `direct` untuk tool destruktif · progressive disclosure (`search_tools`) ·
Docker/gVisor · multi-user · prompt caching blok instruction.

---

## 3. Tech stack

Sudah dikunci. Alasan lengkap, kandidat yang ditolak, dan lapisan isolasi ada di
[`docs/tech-stack.md`](docs/tech-stack.md).

Ringkas: host Python (ADK) · sandbox TypeScript di Deno · stdio + JSON Lines ·
uv + pytest + ruff + pydantic · container di Fase 5.

**Pilihan bahasa sandbox diperlakukan sebagai hipotesis yang diuji, bukan
asumsi.** Tidak ada implementasi code mode mana pun yang menerbitkan
perbandingan TS vs Python; klaim Cloudflare sendiri diawali *"Perhaps this is
because LLMs have an enormous amount of real-world TypeScript in their training
set"*. Karena itu `Codegen` dibuat pluggable dan lengan Python masuk Fase 4.

---

## 4. Komponen & batas tanggung jawab

```
┌─ agent.py (ADK, sinkron) ──────────────────────────────────┐
│  McpToolset(...)  ──────┐                                  │
│  CodeMode(toolsets=[…]) ┤                                  │
│  LlmAgent(                                                 │
│    tools=[codemode.tool],                                  │
│    instruction=base + codemode.instruction,                │
│    after_agent_callback=[codemode.release])                │
└────────────────────────────────────────────────────────────┘

        CodeMode (library)
        ├── Catalog     : BaseTool[] → katalog netral (name/desc/in+out schema)
        ├── Codegen     : katalog → deklarasi API + doc comment  [pluggable: TS | PY]
        ├── ExecuteCodeTool : FunctionTool, satu-satunya yang dilihat model
        ├── Sandbox     : spawn fresh → run → buang (no pool, kecuali S4 memaksa)
        ├── RpcBridge   : permintaan sandbox → BaseTool.run_async
        └── OutputGuard : bounding log, overflow → ADK artifact
```

Lima batas yang harus dijaga bersih:

- `Catalog` tidak tahu soal bahasa target, dan **tidak tahu soal MCP** — dia
  bekerja atas `BaseTool` ADK. `McpTool` hanya kasus yang schema-nya lebih kaya.
- `Codegen` tidak tahu soal ADK
- `Sandbox` tidak tahu soal MCP
- `RpcBridge` satu-satunya yang menyentuh ADK
- `OutputGuard` tidak tahu soal sandbox

Konsekuensi: kalau nanti perlu varian gateway (agnostik framework), hanya
`RpcBridge` + lapisan registrasi yang ditulis ulang.

---

## 5. Alur satu turn

```
1.  Model baca instruction (deklarasi seluruh tool) + 1 tool: execute_code
2.  Model → execute_code(code: "...")
3.  ExecuteCodeTool: spawn sandbox BARU (fresh per call, lalu dibuang)
4.  Kirim kode ke sandbox via stdin
5.  Sandbox jalan, jaringan diputus total
      └ stub tools.<server>.<tool>({...}) → 1 baris JSON ke channel RPC
6.  RpcBridge baca → resolve nama → BaseTool.run_async(args, tool_context)
      └ tool_context yang SAMA dengan execute_code → state & artifact nyambung
7.  Hasil / error → 1 baris JSON balik ke sandbox
      └ error WAJIB di-raise di stub, bukan di-return
8.  Ulangi 5-7 sebanyak call di dalam kode
9.  Skrip selesai → host kumpulkan SEMUA console.log (hanya itu, tanpa
      nilai kembalian — setia blog)
10. OutputGuard: potong kalau lewat batas, overflow → ADK artifact
11. Kembalikan ke model
12. Sandbox dibuang. after_agent_callback → reaper jaring pengaman
```

### Titik kritis: pemetaan error (langkah 7)

ADK mengembalikan kegagalan tool sebagai **nilai** untuk dibaca model. Kode
sandbox membaca **return value**. Kalau host mengembalikan error sebagai nilai
biasa, kode akan lanjut seolah berhasil — bug senyap.

Pemetaan wajib: HTTP error → exception dengan `status_code` + `body`; sisanya →
`ToolError`.

### Titik kritis: pemisahan channel (langkah 9)

`console.log` harus ditangkap di channel yang **terpisah** dari RPC. Kalau
dicampur, log yang kebetulan berbentuk JSON akan mengacaukan parser.

---

## 6. Fase

### Fase 0 — Spike (wajib sebelum menulis komponen apa pun)

> **Status: SELESAI 2026-11-21.** Kelima spike dijalankan. Hasil lengkap, angka,
> dan dampaknya ke plan ada di [`docs/spike-results.md`](docs/spike-results.md).
> Skrip: `spikes/s123_adk.py`, `spikes/s4_host.py`, `spikes/s4_sandbox.ts`.

Lima pertanyaan "bisa/tidak". Hasilnya bisa membatalkan sebagian rencana di
atas, jadi tidak masuk akal menulis Fase 1 sebelum terjawab.

| # | Yang diuji | Hasil | Konsekuensi |
|---|---|---|---|
| S1 | `instruction` ADK bisa provider **async**? | ✅ **YA** | Rencana cadangan `before_model_callback` tidak perlu. Tapi `bypass_state_injection=True` → `{var}` tidak di-inject otomatis |
| S2 | `before_tool`/`after_tool` jalan kalau `run_async()` dipanggil langsung? | ❌ **TIDAK** (hipotesis benar) | `RpcBridge` wajib mereplikasi 5 langkah `functions.py:510-569` |
| S2b | Apakah replikasi itu **mungkin**? | ✅ **YA — 5/5 skenario** | `_invocation_context` terjangkau; `.agent` & `.plugin_manager` field publik. HITL, deny-rule, dan redaksi PII tetap hidup |
| S3 | Seberapa bergantung pada `McpTool._mcp_tool` (privat)? | ✅ **Nyaris tidak** | `_get_declaration().parameters_json_schema` **identik** dengan `_mcp_tool.inputSchema`. Sisa ketergantungan hanya `outputSchema`. `outputSchema` ada di **14/14** tool |
| S4 | Deno: jaringan mati? stdio dua arah? latensi? | ✅ **Semua lolos** | `fetch`/`connect`/`readTextFile` → `NotCapable`. Median **85 ms** end-to-end → **fresh per call, final**. Bonus: `data:` URL import → tanpa transpiler |
| S5 | Pemisahan channel log vs RPC | ✅ **Aman** | Log yang menyerupai pesan protokol tidak menipu parser. Override `console.log` terbukti |

Ditunda ke Fase 4 (bukan Fase 0, karena hanya dibutuhkan lengan pembanding):
spike CPython WASI di wasmtime — apakah `socket`/`subprocess` benar-benar raise,
berapa ms start precompiled, dan apakah `asyncio` jalan.

### Fase 1 — Catalog + Codegen

Output: dari `McpToolset` → deklarasi API yang dibaca model.

Keputusan di fase ini:

- **Penamaan.** Pertahankan nama MCP apa adanya (blog memakai
  `fetch_agents_documentation`, bukan camelCase). Hindari lapisan pemetaan.
- **Namespace.** Blog memakai `declare const codemode: {...}` flat. Dengan >1
  server akan tabrakan → pakai `tools.<server>.<tool>`.
- **Bentuk keluaran.** Tiru blog persis: dua interface per tool plus satu objek
  deklarasi. `outputSchema` **ada di 14/14 tool** server filesystem (S3), jadi
  interface `Output` sungguhan adalah jalur utama — index signature hanya
  fallback.

  ```ts
  interface SearchAgentsCodeInput  { query: string; page?: number }
  interface SearchAgentsCodeOutput { [key: string]: any }
  // ...
  search_agents_code: (input: SearchAgentsCodeInput) => Promise<SearchAgentsCodeOutput>
  ```

  Satu argumen bernama `input`. Return `Promise<Output>`.
  `outputSchema` absen → index signature; jangan dilewatkan — blog tetap
  memancarkan interface Output meski longgar.
- **Abaikan kunci meta.** Schema MCP memakai JSON Schema draft 2020-12 dan
  membawa `$schema` (S3). Jangan sampai diterjemahkan jadi field.
- **Schema → tipe.** Mulai dari subset (object/string/number/boolean/array/enum/
  optional). `oneOf`/`anyOf`/`$ref`/nested `additionalProperties` → tipe longgar
  + doc comment yang menjelaskan.
- **Doc comment** diturunkan dari `description` tool. Jangan dipotong — ini yang
  menggantikan tool description.

Gerbang keluar: `scripts/f1_gate.py` — tiga pemeriksaan, bukan satu:

1. `deno check` atas deklarasi → harus **lolos**
2. `deno check` atas probe **positif** (pemakaian benar) → harus **lolos**
3. `deno check` atas probe **negatif** (tipe salah, field wajib hilang, tool
   tidak ada) → harus **GAGAL**

Langkah 3 yang membuat gerbang ini berarti. Tanpa itu, deklarasi yang isinya
`any` semua juga akan "lolos".

> **Status: LOLOS 2026-11-21.** 14 tool, 234 baris, 8.379 char (~2.1k token).
> Input schema 14/14 dari jalur publik `declaration`; `outputSchema` 14/14.
> Probe negatif gagal dengan 3 error yang tepat: `TS2322` (tipe salah),
> `TS2345` (field wajib hilang), `TS2339` (tool tidak ada). 37 unit test lolos.

#### Temuan Fase 1: deskripsi tool bisa melarikan diri dari blok komentar

Deskripsi `search_files` dari server filesystem **resmi** mengandung
`'**/*.ext'`. Substring `*/` di dalamnya **menutup blok JSDoc lebih awal** dan
merusak seluruh deklarasi — gerbang gagal di percobaan pertama.

Ini bukan cacat kosmetik. Deskripsi datang dari server MCP, jadi server yang
bermusuhan bisa memakai celah yang sama untuk menyuntikkan deklarasi TS buatan
sendiri ke dalam konteks model. `codegen.typescript.escape_doc()` menetralkan
`*/`, dengan test regresi yang membuktikan payload tetap inert setelah semua
blok komentar dibuang.

### Fase 2 — Sandbox + RPC

- Protokol: JSON Lines. Permintaan `{id, server, tool, args}`, balasan
  `{id, ok, value}` atau `{id, ok:false, error:{type, message, ...}}`.
- Stub dibangkitkan sebagai fungsi async yang menulis permintaan dan menunggu
  balasan ber-`id` cocok. Ini yang membuat `await`, paralel, loop, dan try/catch
  bekerja natural.
- **Wajib sejak awal:** wall-clock timeout, batas jumlah call per eksekusi, batas
  ukuran stdout. Model akan menulis loop polling — itu contoh resmi di blog
  Anthropic, anggap pasti terjadi.

Gerbang keluar: skrip yang memanggil 3 tool berantai, memfilter hasil, dan hanya
me-log ringkasannya berhasil; dan upaya akses jaringan di dalam sandbox gagal.

> **Status: LOLOS 2026-11-21.** 28 test sandbox (64 total), `runtime.ts`
> type-check + lint bersih, ruff bersih.

#### Temuan Fase 2: `--deny-net` tidak memblokir module import

Module loader Deno punya allow-list **bawaan** (`esm.sh`, `cdn.jsdelivr.net`,
`raw.githubusercontent.com`, ...). Dengan `--deny-net` saja,
`await import("https://esm.sh/lodash")` **berhasil** dan `import "npm:lodash"`
mengunduh dari registry npm — kanal eksfiltrasi lewat URL.

`--deny-import` tidak memperbaikinya; `--cached-only` lebih buruk. Perbaikan:
**`--no-remote --no-npm`**. Detail di `docs/tech-stack.md`.

#### Pesan protokol BISA dipalsukan kode model

`console.log` dibajak, tapi `Deno.stdout.writeSync` tidak — jadi kode model
dapat menulis pesan `call`/`done`/`log` palsu langsung ke stdout.

Dianalisis, bukan diabaikan:
- `call` palsu tetap **dihitung** host dan tetap melewati `RpcBridge`, jadi
  kebijakan dan batas tetap berlaku. Bukan eskalasi hak.
- `done` palsu hanya menghentikan eksekusinya sendiri. Merugikan diri sendiri.
- `log` palsu hanya menambah log.

Konsekuensi desain: **semua batas ditegakkan di host**, bukan di sandbox.
Penjaga sisi sandbox hanya untuk gagal-cepat. Ada test yang memalsukan 10 `call`
lewat stdout mentah dan membuktikan host tetap menutup di `max_calls`.

### Fase 3 — Integrasi ADK

- `ExecuteCodeTool` sebagai `FunctionTool` biasa.
- `McpToolset` dideklarasikan **sinkron** di `agent.py` (aturan deployment ADK).
- **`RpcBridge` wajib mereplikasi rantai callback.** S2 membuktikan
  `tool.run_async()` langsung **tidak** memicu callback apa pun — callback
  dipicu oleh flow di `functions.py:510-569`. Urutan yang harus ditiru:

  1. `plugin_manager.run_before_tool_callback(tool, tool_args, tool_context)`
  2. `agent.canonical_before_tool_callbacks` — `await` kalau awaitable,
     **break** begitu ada hasil
  3. `tool.run_async(...)` + `on_tool_error` callbacks saat gagal
  4. `plugin_manager.run_after_tool_callback(..., result=)`
  5. `agent.canonical_after_tool_callbacks`

  Kalau langkah 1-2 mengembalikan nilai, tool **tidak** dijalankan — inilah yang
  nanti membuat `require_confirmation` / HITL tetap hidup.

  **Prasyarat: TERPENUHI (S2b).** `tool_context._invocation_context` terjangkau,
  dan `.agent` + `.plugin_manager` adalah field **publik**. Lima skenario
  terbukti: rantai lengkap · `before` membatalkan tool (dasar HITL) · `after`
  meredaksi hasil · kegagalan ter-raise · `on_tool_error` menangani.

  **Dua jebakan yang sudah ketemu:**
  - `on_tool_error`: ADK menjalankan **plugin dulu, lalu canonical**
    (`functions.py:454-477`). Jangan dibalik.
  - `args` wajib di-`deepcopy` sebelum masuk rantai (`functions.py:482`) —
    callback boleh memutasinya.

  **Atribusi: SELESAI.** `derive_tool_context()` membuat ToolContext turunan
  per tool call dengan `function_call_id` berbeda (`fc-exec#1`, `#2`, ...),
  **tapi `event_actions` dibagi dengan parent**. Yang kedua itu wajib: kalau
  turunan membuat `EventActions` baru, state delta dan artifact delta dari tool
  di dalam sandbox **hilang diam-diam**, karena flow hanya mempersistensi
  EventActions milik `execute_code`.

> **Status Fase 3: LOLOS 2026-11-21.** `scripts/f3_gate.py` - tumpukan penuh
> dengan MCP server filesystem sungguhan, 6 tool call berantai, filter di dalam
> kode. Audit callback melihat keenamnya dengan `function_call_id` unik.
> 83 test lolos.

#### Temuan Fase 3a: tipe `Output` tidak cocok dengan nilai runtime

`outputSchema` MCP menjelaskan isi `structuredContent`, tapi
`McpTool.run_async` meneruskan **envelope `CallToolResult` utuh**:

```json
{"content":[{"type":"text","text":"..."}],
 "structuredContent":{"content":"..."},
 "isError":false}
```

Codegen Fase 1 memakai `outputSchema` apa adanya, jadi tipe yang dideklarasikan
mengatakan `content: string` sementara nilai runtime-nya **array blok**. Model
akan menulis `r.content` mengharapkan string. **Tipe yang menyesatkan lebih
buruk daripada tanpa tipe.**

Diperbaiki dengan `catalog.mcp_envelope()` - pengetahuan MCP ditaruh di
`Catalog` (yang memang sudah tahu MCP), sehingga `Codegen` tetap generik.

Biaya akurasi ini terukur: deklarasi tumbuh **8.379 -> 14.211 char**
(~2,1k -> ~3,6k token) karena envelope dipancarkan per tool. Optimasi yang
jelas untuk Fase 5: satu tipe generik bersama
(`interface McpResult<T> { ... }`) alih-alih mengulang envelope 14 kali.
Dicatat dengan angka, bukan dikerjakan sekarang, supaya Fase 4 tidak tertunda.

#### Temuan Fase 3b: `isError` MCP tidak melempar exception

Terukur pada server sungguhan - membaca berkas yang tidak ada:

```json
{"content":[{"type":"text","text":"ENOENT: no such file..."}], "isError":true}
```

`McpTool.run_async` **tidak** melempar exception. Di direct-mode itu tidak
berbahaya: model membaca teksnya dan paham. Di code mode **fatal**, karena kode
sandbox membaca return value dan lanjut seolah berhasil.

Plan sudah mengantisipasi jebakan "error sebagai nilai" di lapisan ADK, tapi
**melewatkan lapisan envelope MCP**. `RpcBridge` sekarang melempar
`McpToolFailed`.

Penempatannya disengaja: **setelah** seluruh rantai callback, bukan sebelum.
Di direct-mode ADK memperlakukan hasil ber-`isError` sebagai hasil sukses, jadi
callback memang harus tetap melihatnya - dan callback redaksi masih bisa
membersihkan `isError` lalu membuat call itu dianggap berhasil. Ada test untuk
kedua perilaku.

#### Temuan Fase 3c: `require_confirmation` tersimpan di atribut privat

ADK menyimpannya sebagai `_require_confirmation`; nama publik hanya ada di
parameter `__init__`. Memeriksa nama publik saja akan **selalu** melewatkan tool
yang butuh konfirmasi - ditangkap oleh test, bukan oleh pembacaan kode.

Sekalian ditambahkan: tool dengan `_credentials_manager` juga ditolak, karena
`run_async`-nya mengembalikan string `"Pending User Authorization."` sebagai
**nilai** - jebakan gagal-senyap yang sama.
- `after_agent_callback` → `release(invocation_id)`, plus reaper idle sebagai
  jaring pengaman kalau callback tidak terpanggil (crash, cancel).
- Tool yang **harus ditolak** dengan error terstruktur saat membangun katalog
  (bukan saat runtime): butuh credential request, confirmation, UI widget, agent
  transfer, atau escalation.

### Fase 4 — Harness evaluasi

Bukan opsional, bukan belakangan. Code mode **regresi secara senyap**: tetap
jalan, hasilnya diam-diam lebih buruk.

Dua konfigurasi agent (direct-mode vs code-mode), tool dan model identik.

#### Harness harus menguji KETIGA klaim blog

Blog mengklaim tiga hal, dan dua di antaranya mudah terlewat:

> "agents are able to handle **many more tools**, and **more complex tools**, when
> those tools are presented as a TypeScript API rather than directly"

> "The approach really shines when an agent needs to **string together multiple
> calls**"

| Klaim | Cara menguji |
|---|---|
| Lebih banyak tool | Dimensi **jumlah tool**: 10 / 30 / 60 (mock server) |
| Tool lebih kompleks | Dimensi **kompleksitas schema**: flat / nested / `oneOf`+enum besar |
| Rantai multi-call | Komposisi task di bawah |

`server-filesystem` **tidak bisa** menguji dua klaim pertama — ~11 tool, schema
satu-dua field. Karena itu dibutuhkan **mock MCP server berparameter** (jumlah
tool × kompleksitas schema). `server-filesystem` tetap dipakai untuk realisme
pada task rantai dan data-heavy.

#### Metrik

| Metrik | Sumber |
|---|---|
| Input/output token per task | `usage_metadata` di event ADK |
| **Akurasi pemilihan tool** | tool yang benar dipanggil? — **inilah yang sebenarnya diklaim blog** saat tool banyak |
| Jumlah round-trip model | hitung event |
| Wall clock | timer |
| Correctness | assertion otomatis per task |
| Jumlah tool call sebenarnya | hitung di `RpcBridge` |

Akurasi pemilihan tool adalah metrik yang paling mudah dilupakan dan paling
dekat dengan klaim blog: yang runtuh saat katalog membesar bukan jumlah token,
tapi kemampuan model memilih tool yang tepat.

#### Komposisi task (minimal 8, semua punya jawaban terverifikasi)

- 2 single-call sederhana → **ekspektasi: code mode kalah.** Harus kalah; kalau
  menang, pengukurannya salah.
- 3 multi-call berantai (output A → input B → input C) → medan utama
- 2 data-heavy (ambil banyak baris, filter, laporkan ringkasan) → penghematan
  terbesar
- 1 control-flow (loop dengan kondisi berhenti)

Setiap task dijalankan di **setiap titik** grid jumlah-tool × kompleksitas, agar
klaim 1 dan 2 terukur, bukan diasumsikan.

**Gerbang promosi:** code mode dipakai hanya kalau correctness tidak turun dan
ada penghematan token nyata pada kategori 2–4. Tanpa gerbang ini, compile-mode
regresi tanpa terdeteksi.

#### Lengan kedua: TS vs Python

Setelah lengan TS menghasilkan angka, tambahkan lengan Python (`Codegen` kedua +
adapter sandbox wasmtime/WASI) dan jalankan task yang **identik**. Tool, model,
dan prompt sama; hanya bahasa sandbox yang berbeda.

Ini angka yang belum diterbitkan siapa pun. Biayanya ~1,4x, bukan 2x, karena
`Catalog`/`RpcBridge`/`OutputGuard`/harness dipakai bersama.

Catatan desain untuk lengan Python: dengan RPC **sinkron memblokir**, sandbox
tidak butuh `asyncio` sama sekali — model menulis Python sinkron biasa, dan
masalah top-level `await` hilang. Harganya: tidak ada tool call paralel. Kalau
Fase 4 menunjukkan paralelisme penting, tambahkan API batch eksplisit
(`tools.batch([...])`) yang dijalankan paralel di host.

### Fase 5 — Pengerasan

Urutan ditentukan hasil Fase 4, bukan asumsi: jalur `direct` untuk tool
destruktif → container hardening kalau akan dipakai orang lain → progressive
disclosure kalau katalog >50 tool → prompt caching blok instruction.

#### Fase 5a — jalur `direct` (SELESAI)

Masalahnya bukan hipotetis. Di code mode, `write_file` dipanggil dari dalam
kode yang ditulis model, jadi argumennya **tidak pernah dilihat siapa pun**
sebelum berjalan, dan `require_confirmation` ADK tidak punya tempat untuk
bertanya. Sebelum fase ini tool semacam itu hanya **dibuang** dari katalog —
hilang diam-diam dari sudut pandang model.

Keputusan: memilah, bukan melarang. Tool destruktif dikeluarkan dari sandbox
dan dideklarasikan apa adanya ke model, sehingga HITL, rantai callback, dan
alur kredensial berjalan lewat jalur bawaan ADK — bukan lewat replikasi kami di
`RpcBridge`. Polanya jadi: `execute_code` untuk membaca dan menyaring, tool
langsung untuk bertindak.

Permukaan API: `CodeMode(..., direct=[...] | predikat,
direct_policy=DirectPolicy)` dan `cm.tools` = `[cm.tool, cm.direct_toolset]`.
Toolset, bukan list, karena isinya baru diketahui setelah `prepare()` yang
asinkron sementara `LlmAgent(tools=[...])` dibangun sinkron. Tanpa tool
destruktif, `direct_toolset` kosong dan konteks model tetap berisi tepat satu
tool seperti di blog.

**Tiga kebijakan, default `off`.** Default sengaja tidak mengubah apa pun dari
Fase 0-4, supaya angka evaluasi yang sudah terbit tetap berlaku.

Terukur pada server filesystem resmi — 14/14 tool beranotasi:

| kebijakan | direct | tool |
|---|---|---|
| `off` | 0/14 | — |
| `annotations` | 3/14 | `write_file`, `edit_file`, `move_file` |
| `annotations-strict` | 3/14 | sama |

`create_directory` **tetap di sandbox** karena anotasinya
`destructiveHint=false` (aditif). Itu memang semantik yang dimaksud spec, dan
kebetulan membuktikan heuristik berbasis nama ("mengandung *create* berarti
berbahaya") akan salah di sini.

**Dua jebakan spesifikasi MCP yang hampir terlewat:**

1. `destructiveHint` **default-nya true**, dan hanya bermakna saat
   `readOnlyHint == false`. Jadi "anotasi absen" tidak berarti "aman".
   `annotations` memilih pragmatis (absen → tetap di sandbox) karena banyak
   server tidak menulis anotasi; `annotations-strict` setia pada spec dan
   karena itu bisa mengosongkan sandbox sepenuhnya. Di server resmi keduanya
   sama — perbedaannya baru muncul pada server yang anotasinya tidak lengkap.
2. Spec menyatakan anotasi adalah **hint dari server yang mungkin tidak
   dipercaya**: *"Clients should never make tool use decisions based on
   ToolAnnotations received from untrusted servers."* Karena itu anotasi di
   sini hanya boleh **memperketat**, tidak pernah melonggarkan —
   `readOnlyHint=true` tidak bisa menarik tool keluar dari daftar `direct`
   yang diminta pemilik agent. Urutannya dipaksa di `direct_reason()`:
   eksplisit → batasan teknis → anotasi.

**Penegakan di host, dua lapis.** Nama tool direct tidak ada di manifest
maupun di deklarasi TypeScript, jadi jalur normal gagal sebagai `TypeError` di
sandbox. Tapi itu tidak cukup: kode model bisa memalsukan pesan `call` lewat
`Deno.stdout` mentah. Jadi `RpcBridge` juga menolaknya dengan `DirectOnlyError`,
dan `_tool_map` dibangun dari `catalog.tools` — bukan dari seluruh sumber.

Gerbangnya bisa gagal, dan dibuktikan: filter `_tool_map` disabotase →
`test_direct_ditolak_host_walau_protokol_dipalsukan` dan
`test_bridge_menolak_tool_direct_secara_langsung` keduanya **gagal**. Total
test naik 84 → 93.

`scripts/f5_gate.py` menjalankan hal yang sama di server filesystem resmi
(5 pemeriksaan). Hasil sabotase di sana lebih tegas dari yang diperkirakan:
pemalsuan protokol **benar-benar menulis `diretas.txt` ke disk**, dengan
`before_tool_callback` melihat `write_file` berjalan. Jadi lapisan manifest
sendirian memang tidak cukup — risikonya terukur, bukan teoretis.

Temuan yang mengubah urutan prioritas: penegakan di manifest terlihat cukup
saat dibaca, dan hanya gerbang yang bisa gagal yang menunjukkan bahwa ia
tidak cukup. Ini pengulangan pelajaran S2 (callback tidak jalan tanpa suara)
di konteks berbeda.

Catatan kesalahan yang sempat terjadi di fase ini: versi pertama test pemalsuan
protokol memakai `assert not any(... for t in [])` — assertion yang **tidak
bisa gagal**, kesalahan yang sama persis dengan dua test cacat di Fase 2.
Diganti dengan audit `before_tool_callback` yang memeriksa tool direct tidak
pernah masuk rantai callback.

#### Fase 5b — container hardening (belum)

Belum dikerjakan secara sadar, bukan karena lupa: daemon Docker tidak hidup di
mesin ini, dan menulis jalur container yang tidak bisa dijalankan berarti
membuat gerbang yang **tidak bisa gagal** — hal yang berulang kali jadi sumber
kesalahan di proyek ini.

---

## 7. Risiko

| Risiko | Mitigasi |
|---|---|
| ~~`_mcp_tool` privat pecah saat upgrade ADK~~ **diturunkan** | S3: `_get_declaration().parameters_json_schema` **identik** dengan `_mcp_tool.inputSchema` (butuh `JSON_SCHEMA_FOR_FUNC_DECL=True`). Input aman tanpa API privat. Sisa ketergantungan hanya `outputSchema` → degradasi mulus ke index signature |
| ~~Callback/HITL tidak ikut jalan~~ **teratasi** | S2 membuktikan masalahnya, **S2b membuktikan solusinya** (5/5). Replikasi 5 langkah layak; prasyarat akses terpenuhi. Sisa: atribusi `function_call_id` per tool call (Fase 3) |
| ~~Spawn sandbox lambat di Windows~~ **selesai** | S4: median 85 ms end-to-end. Fresh per call. Catatan: pakai `deno.exe` langsung, bukan shim `.cmd` npm |
| ~~Transpile TS → JS + sourcemap~~ **hilang** | S4: `data:` URL import ditranspile Deno sendiri, dengan semua permission ditolak |
| Codegen gagal pada schema rumit | Subset dulu, tipe longgar + doc comment untuk sisanya |
| Instruction dari provider tidak kena state injection | S1: `bypass_state_injection=True` untuk semua provider. Tidak masalah di v0 (deklarasi statis); kalau perlu, panggil `inject_session_state()` sendiri |
| Model menulis loop tak berujung | Timeout + batas call, sejak Fase 2 |
| Discovery cost membengkak | Tidak relevan di v0 (upfront). Baru muncul kalau progressive disclosure dipakai — Codex terukur kena floor ~40k token |

---

## 8. Catatan: varian gateway (tidak dipilih sekarang)

Kalau kebutuhan agnostik framework ternyata lebih penting daripada identitas
per-user, `CodeMode` bisa dibungkus menjadi **MCP server** yang mengekspos
`search_tools` / `describe_tool` / `execute_code`. Semua framework yang bicara
MCP (ADK `McpToolset`, LangChain `MCPAdapter`, Claude Code, Codex, pi, OpenCode)
langsung memakainya tanpa adapter.

Yang hilang: callback framework, HITL/confirmation, state & artifacts,
identitas kredensial per-user, trace yang menyambung, elicitation/sampling/roots.

Batas komponen di §4 dirancang supaya perpindahan ini tidak menuntut penulisan
ulang `Catalog`/`Codegen`/`Sandbox`.

---

## 9. Penyimpangan & tambahan terhadap blog

Klaim "setia blog" hanya jujur kalau selisihnya dicatat. Hasil audit
2026-11-21 terhadap teks blog.

### Sesuai (12)

Konversi schema → API TypeScript · doc comment dari schema · nama tool
dipertahankan apa adanya · API dimuat ke konteks agent · seluruh API upfront ·
satu tool eksekusi kode · sandbox terputus total dari internet · `fetch()`
melempar error · RPC callback ke agent loop · supervisor pemegang token ·
kredensial tidak bisa bocor ke kode · `codemode({system, tools})` →
`{system, tools}`.

### Penyimpangan sadar (4)

| # | Blog | Kita | Alasan |
|---|---|---|---|
| D1 | Isolate V8, dikeraskan infra Cloudflare | Deno + container (Fase 5b, belum) | `workerd` lokal bukan hardened sandbox menurut dokumen Cloudflare sendiri; isolasi kuat adalah properti infra mereka |
| D2 | Bindings (`env` berisi objek hidup) | stdio ke parent | Semangat sama (kapabilitas, bukan jaringan) dan sedikit lebih kuat: tanpa stack jaringan sama sekali. Harganya: perlu lapisan serialisasi sendiri |
| D3 | `declare const codemode` flat | `tools.<server>.<tool>` | Tabrakan nama dengan >1 server. **Untuk run eval satu-server, pakai flat** agar tidak menambah indireksi yang blog tidak punya |
| D4 | Tool generik, "MCP servers included" | MCP-only (scope proyek) | Penyempitan yang diminta. Diperkecil dampaknya dengan `Catalog` berbasis `BaseTool` — menambah `FunctionTool` nanti nyaris gratis |

### Tambahan di luar blog (6)

Blog **diam** soal keenam hal ini. Semuanya celah nyata, bukan kelalaian kita —
blog tidak membahas tool destruktif, HITL, maupun apa yang terjadi saat model
menulis loop tak berujung.

timeout & batas jumlah call · bounding output + artifact · pemetaan error (raise
vs return) · pemilahan tool yang tidak boleh masuk sandbox · harness evaluasi ·
jalur `direct` (Fase 5a, sudah ada)

### Sudah dikoreksi dari draf awal

| Temuan | Koreksi |
|---|---|
| "nilai ekspresi terakhir" dibawa dari Code Mode **Claude Code**, bukan blog | Dihapus. Hanya `console.log`. Menyimpannya akan jadi *confound*: kalau code mode menang, tidak jelas karena polanya atau karena ergonomi nilai kembalian |
| Lifecycle per-turn melawan "no need to reuse them" dan belum diukur | Default dibalik ke fresh per call; reuse hanya kalau S4 memaksa |
| `outputSchema` tidak ditangani codegen | Fase 1 kini memancarkan interface `Input` **dan** `Output`, index signature saat `outputSchema` absen |
| Fase 4 hanya menguji 1 dari 3 klaim blog | + dimensi jumlah tool (10/30/60), + dimensi kompleksitas schema, + metrik akurasi pemilihan tool, + mock server berparameter |
| `Catalog` terikat `McpTool` | Dikunci pada `BaseTool`; memperlemah risiko S3 sekaligus mendekati scope blog |
