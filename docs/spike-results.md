# Fase 0 — Hasil Spike

Dijalankan 2026-11-21. Windows, Python 3.12.9, google-adk 1.33.0, mcp 1.26.0,
Deno 2.9.6 (V8 15.0.245.2, TypeScript 6.0.3), Node v22.15.0.

Skrip: `spikes/s123_adk.py` (S1-S3), `spikes/s4_host.py` + `spikes/s4_sandbox.ts`
(S4-S5).

| # | Pertanyaan | Hasil | Dampak ke plan |
|---|---|---|---|
| S1 | `instruction` boleh provider async? | ✅ YA | Tidak perlu `before_model_callback`. Ada catatan state injection |
| S2 | `before_tool`/`after_tool` jalan saat `run_async()` langsung? | ❌ **TIDAK** | `RpcBridge` harus mereplikasi 5 langkah |
| S2b | Apakah replikasi itu MUNGKIN? | ✅ **YA, terbukti 5/5** | Prasyarat terpenuhi. HITL & redaksi tetap hidup |
| S3 | Seberapa bergantung pada API privat? | ✅ Nyaris tidak | Risiko diturunkan; `outputSchema` ternyata ada |
| S4 | Deno: jaringan mati, RPC dua arah, latensi | ✅ Semua lolos | **Fresh per call dikonfirmasi**, bukan lagi kondisional |
| S5 | Pemisahan channel log vs RPC | ✅ Aman | Teknik override `console.log` terbukti |

---

## S1 — `instruction` provider async: DIDUKUNG

`LlmAgent.canonical_instruction()` (llm_agent.py:553-575) meng-`await` hasil
provider:

```python
instruction = self.instruction(ctx)
if inspect.isawaitable(instruction):
    instruction = await instruction
return instruction, True
```

Terbukti empiris: provider `async def` dipanggil tepat sekali, hasilnya sampai.

### Yang penting dan tidak terduga

Nilai kedua yang dikembalikan adalah `bypass_state_injection=True` untuk semua
provider. Artinya **`{var}` tidak di-inject otomatis** saat instruction berasal
dari provider, berbeda dari instruction berupa string.

Konsekuensi: kalau blok instruction kita perlu state session, panggil
`google.adk.utils.instructions_utils.inject_session_state()` sendiri. Untuk v0
tidak dibutuhkan — deklarasi TS statis per katalog.

**Rencana cadangan (`before_model_callback`) tidak diperlukan.**

---

## S2 — Callback TIDAK jalan: hipotesis plan benar

Dibuktikan dua arah:

- Empiris: `before_tool_callback` + `after_tool_callback` dipasang di agent,
  `tool.run_async()` dipanggil langsung → **tidak satu pun terpanggil**
  (`canonical_before=1`, `canonical_after=1`, jadi bukan salah wiring).
- Source: callback dipicu oleh **flow**, di
  `flows/llm_flows/functions.py:510-569`, bukan oleh tool. `tool.run_async()`
  ada di dalam `__call_tool_async` yang dipanggil *setelah* callback.

### Urutan yang harus direplikasi `RpcBridge`

Persis seperti `functions.py:510-569`:

1. `invocation_context.plugin_manager.run_before_tool_callback(tool, tool_args, tool_context)`
2. `agent.canonical_before_tool_callbacks` — iterasi, `await` kalau awaitable,
   **break** begitu ada hasil
3. `tool.run_async(args=, tool_context=)` — bungkus `try/except`, saat gagal
   jalankan `on_tool_error` callbacks
4. `plugin_manager.run_after_tool_callback(..., result=)`
5. `agent.canonical_after_tool_callbacks`

Kalau salah satu langkah 1-2 mengembalikan nilai, tool **tidak** dijalankan —
dan inilah yang membuat `require_confirmation` / HITL bisa tetap hidup nanti.

### Konsekuensi

`RpcBridge` butuh akses ke `invocation_context` dan `agent`, bukan hanya
`tool_context`. Perlu dicek saat Fase 3 apakah keduanya terjangkau dari
`ToolContext` yang diterima `ExecuteCodeTool`.

---

## S3 — Ketergantungan API privat jauh lebih kecil dari dugaan

Diuji terhadap `@modelcontextprotocol/server-filesystem`: **14 tool**.

### Temuan 1: `outputSchema` ADA di 14/14 tool

Plan mengasumsikan `outputSchema` sering absen sehingga interface `Output` cukup
index signature. **Salah.** Server filesystem menyediakannya untuk semua tool.

→ Codegen harus memancarkan interface `Output` **sungguhan**. Index signature
hanya fallback, bukan jalur utama.

### Temuan 2: jalur publik memberi schema yang IDENTIK

```
_get_declaration().parameters_json_schema  ==  _mcp_tool.inputSchema   -> True
```

Syarat: `override_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, True)`.
Tanpa flag itu, `parameters_json_schema` bernilai `None` dan yang terisi hanya
`parameters` (bentuk Gemini, lossy).

→ **Input schema tidak perlu API privat sama sekali.**

### Temuan 3: sisa ketergantungan privat hanya `outputSchema`

`FunctionDeclaration` ADK tidak membawa output schema, jadi interface `Output`
tetap butuh `_mcp_tool.outputSchema`.

Tapi degradasinya mulus: kalau `_mcp_tool` hilang di rilis ADK berikutnya,
`Output` jatuh ke index signature dan **bridge tetap jalan**. Input — yang
menentukan benar-tidaknya pemanggilan — aman.

→ Risiko S3 diturunkan dari "bisa mematikan codegen" menjadi "menurunkan
kualitas tipe output".

### Temuan 4: schema memakai JSON Schema draft 2020-12

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "tail": { "type": "number", "description": "...last N lines..." },
    "head": { "type": "number", "description": "...first N lines..." }
  },
  "required": ["path"]
}
```

Codegen harus **mengabaikan `$schema`** dan kunci meta lain, jangan sampai
diterjemahkan jadi field.

---

## S4 — Sandbox Deno: semua lolos

Flag: `--no-prompt --deny-net --deny-write --deny-env --deny-ffi --deny-run
--deny-sys` (tanpa `--allow-*` sama sekali).

| Kasus | Hasil |
|---|---|
| `fetch("https://example.com")` | ❌ `NotCapable` |
| `Deno.connect({hostname, port})` | ❌ `NotCapable` |
| `Deno.readTextFile("C:/Windows/win.ini")` | ❌ `NotCapable` |
| Rantai 2 call, hasil difilter sebelum di-log | ✅ `done` |
| `Promise.all` 2 call — id-matching | ✅ benar, tidak tertukar |
| Error tool → `throw`, bukan nilai | ✅ `ToolError` ter-raise, `catch` jalan |
| TS native (`interface`, type predicate, `filter`) | ✅ tanpa transpiler |

### Temuan besar: eksekusi string TS tanpa transpiler

`new Function(code)` hanya menerima JS, jadi awalnya tampak butuh esbuild/swc —
beserta seluruh masalah sourcemap yang sudah dicatat di `tech-stack.md`.

Ternyata tidak perlu:

```ts
const url = "data:application/typescript;base64," + btoa(...);
await import(url);
```

Deno mentranspile TS dari `data:` URL **dan ini jalan dengan semua permission
ditolak** — tanpa `--allow-read`, tanpa file temporer. `tools` disuntikkan lewat
`globalThis` sebelum import.

Bonus: `deno run` tidak type-check secara default, jadi type error pada kode
model tidak memblokir eksekusi — perilaku yang kita mau untuk skrip sekali pakai.

### Latensi: fresh per call DIKONFIRMASI

End-to-end (spawn + import + eksekusi + teardown), kode minimal, 5x:

```
min=82ms  median=85ms  max=128ms      (spawn saja: 6-20ms)
```

Ambang plan 150 ms → **lolos.** Lifecycle fresh-per-call dipertahankan, setia
blog ("create a new one for every piece of code... throw it away"). **Tidak ada
pool yang perlu dibangun.**

Catatan metodologi: `deno` dari npm di Windows adalah shim `.cmd` yang (a) tidak
terlihat oleh `CreateProcess` dan (b) akan melebih-lebihkan latensi. Pakai
`deno.exe` langsung — di sini
`%APPDATA%\npm\node_modules\deno\deno.exe`.

---

## S5 — Pemisahan channel: aman

`console.log` dibajak di preamble sandbox menjadi pesan RPC bertipe:

```ts
console.log = (...args) => send({ t: "log", data: ... });
```

Uji serangan: kode model me-log string yang **menyerupai pesan protokol**.

```ts
console.log(JSON.stringify({ t: "call", id: 999, server: "evil", tool: "x" }));
console.log('{"t":"done"}');
console.log("sesudahnya masih hidup");
```

Hasil: ketiganya sampai sebagai `data` di dalam pesan `log`. Parser host tidak
tertipu — `calls=0` (tidak ada tool dipanggil), `status=done` baru muncul di
akhir sungguhan, dan baris ketiga terbukti eksekusi berlanjut.

Kunci: host **tidak pernah** membaca stdout mentah sebagai protokol. Semua
keluaran sudah dibungkus di sisi sandbox.

---

---

## S2b — Perbaikan S2: replikasi callback TERBUKTI JALAN

S2 hanya membuktikan masalahnya. S2b membuktikan solusinya, lewat
`spikes/s2b_callback_chain.py`.

### Prasyarat: terpenuhi

Rantai aksesnya pendek dan dua dari tiga mata rantainya publik:

```
tool_context._invocation_context      # privat, tapi dipakai ADK sendiri di mana-mana
    .agent                            # field PUBLIK (invocation_context.py:164)
    .plugin_manager                   # field PUBLIK (invocation_context.py:212)
```

Terbukti: `tool_context._invocation_context is ictx` -> `True`.

Catatan versi: di ADK 1.33.0, `ToolContext` adalah **alias** untuk
`google.adk.agents.context.Context` (`tool_context.py:29`). `Context` mewarisi
`_invocation_context` dari `ReadonlyContext`.

### Lima skenario, semua lolos

| # | Skenario | Urutan yang terbukti | Artinya |
|---|---|---|---|
| 1 | Rantai lengkap | `before > TOOL > after` | Audit per-tool hidup |
| 2 | `before` mengembalikan nilai | `before MENOLAK > tool dibatalkan > after` | **Dasar HITL / deny-rule** |
| 3 | `after` mengubah hasil | `before > TOOL > after MEREDAKSI` | **Dasar redaksi PII** |
| 4 | Tool gagal, tak ada handler | `before > TOOL raise > RAISE ke pemanggil` | Sandbox melihat exception |
| 5 | `on_tool_error` menangani | `... > on_tool_error MENANGANI > after` | Jadi nilai, bukan exception |

Skenario 4 adalah yang paling penting untuk korektness: ADK mengembalikan
kegagalan tool sebagai **nilai**, tapi kode sandbox membaca **return value** —
jadi bridge wajib me-`raise`, bukan me-return. Terbukti bekerja.

### Dua detail yang hampir salah

**Urutan `on_tool_error` terbalik.** Implementasi pertama saya menjalankan
canonical lalu plugin. ADK melakukan sebaliknya (`functions.py:454-477`):
plugin **dulu**, baru canonical, dan `return` begitu salah satu memberi hasil.
Kalau dibalik, plugin observability bisa terlewat.

**`args` harus di-deepcopy.** ADK melakukannya di `functions.py:482` karena
callback boleh memutasi args. Argumen yang datang dari sandbox juga harus
diisolasi sebelum masuk rantai callback.

### Urutan final untuk `RpcBridge`

```
args = deepcopy(args)
1. plugin_manager.run_before_tool_callback(...)
2. agent.canonical_before_tool_callbacks        -> break kalau ada hasil
3. tool.run_async(...)
     on error: plugin_manager.run_on_tool_error_callback(...)   <- plugin DULU
               lalu agent.canonical_on_tool_error_callbacks
               kalau dua-duanya None -> RAISE (jangan return)
4. plugin_manager.run_after_tool_callback(..., result=)
5. agent.canonical_after_tool_callbacks
```

### Yang masih harus diuji di Fase 3

S2b memakai `InvocationContext` yang dibangun manual, bukan dari `Runner`
sungguhan. Yang belum terbukti:

- apakah `ToolContext` yang diterima `ExecuteCodeTool` dari flow sungguhan
  membawa `_invocation_context` yang sama lengkap
- apakah memakai ulang `tool_context` yang sama untuk tool di dalam sandbox
  menimbulkan masalah atribusi (`function_call_id` milik `execute_code`, bukan
  milik tool yang dipanggil di dalamnya)

Yang kedua kemungkinan besar perlu `ToolContext` turunan per tool call, dengan
`function_call_id` sendiri. Diputuskan saat Fase 3.

## Yang berubah di `implementasi.md`

| # | Perubahan |
|---|---|
| 1 | Lifecycle: dari "kondisional pada S4" → **fresh per call, final** |
| 2 | Fase 1: interface `Output` jadi jalur utama (bukan index signature); abaikan `$schema` |
| 3 | Fase 1/2: catat teknik `data:` URL — transpiler dihapus dari daftar risiko |
| 4 | Fase 3: tambahkan urutan replikasi 5 langkah + kebutuhan `invocation_context` & `agent` |
| 5 | Risiko: S3 diturunkan; risiko transpile/sourcemap dihapus |
| 6 | S1: catatan `bypass_state_injection` |
| 7 | S2b: urutan final `RpcBridge` + 2 jebakan (urutan on_tool_error, deepcopy args) |

## Sisa yang belum terjawab (masuk Fase 3)

- Apakah `invocation_context` dan `agent` terjangkau dari `ToolContext` yang
  diterima `ExecuteCodeTool`? Ini menentukan apakah replikasi callback S2
  mungkin sama sekali.
- Apakah `tool_context` yang sama boleh dipakai ulang untuk tool di dalam
  sandbox (state & artifact nyambung, atribusi benar).
