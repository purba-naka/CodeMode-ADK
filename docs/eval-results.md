# Fase 4 — Hasil Evaluasi

Dijalankan 2026-11-21. Model: `gemini-3.5-flash`. ADK 1.33.0, Deno 2.9.6.

> **Pilihan model memengaruhi hasil.** Dicatat di sini, bukan dipilih
> diam-diam. `gemini-2.0-flash` sudah dihapus dari API saat pengujian ini.

## Yang diuji

Blog Cloudflare mengklaim **tiga** hal:

> "agents are able to handle **many more tools**, and **more complex tools**,
> when those tools are presented as a TypeScript API rather than directly"

> "The approach really shines when an agent needs to **string together multiple
> calls**"

`@modelcontextprotocol/server-filesystem` (~11 tool, schema satu-dua field)
tidak bisa menguji dua klaim pertama. Karena itu dibuat
`eval/mock_server.py` dengan dua knob:

- `--tools 10|30|60` — jumlah tool
- `--schema flat|nested|oneof` — kompleksitas schema

Tool inti selalu ada dan namanya tetap di semua titik grid, jadi perbandingan
antar titik sahih. Sisanya pengecoh dengan nama dan deskripsi yang masuk akal
(`search_invoices`, `get_customer_orders`, ...), bukan `tool_1`/`tool_2` —
nama yang jelas-jelas sampah membuat pemilihan tool terlalu mudah dan hasilnya
tidak berarti.

---

## Bagian A — Biaya konteks (tanpa LLM)

Sumber: `eval/diag_declared_tools.py`. Deterministik, reproducible, tidak
butuh LLM.

Diukur dari **deklarasi yang benar-benar dibangun ADK** (`LlmRequest` disusun
lewat `tool.process_llm_request()`, sama seperti flow), ditambah panjang
instruction. Apel-ke-apel: direct mode membayar di deklarasi tool, code mode
membayar di instruction.

### Validasi sumbu grid lebih dulu

Sebelum angkanya berarti, satu hal harus dibuktikan: apakah ADK benar-benar
mendeklarasikan sebanyak tool yang diminta?

| mode | 10 | 30 | 60 |
|---|---:|---:|---:|
| direct, tool dideklarasikan | 10 | 30 | 60 |
| code, tool dideklarasikan | 1 | 1 | 1 |

Ya. Direct mode mendeklarasikan tepat sejumlah grid; code mode tepat satu
(`execute_code`, 484 char, **konstan** di semua titik grid). **Sumbu
jumlah-tool sahih.**

Ini perlu diperiksa karena catatan kabel sebelumnya menunjukkan
`n_tools_declared: 1` dan sempat memunculkan dugaan bahwa ADK memotong daftar
tool - yang kalau benar akan membuat seluruh grid tidak mengukur apa pun.

### Hasil

Total konteks dalam char (deklarasi + instruction):

| schema | tool | direct | code | rasio |
|:---|---:|---:|---:|---:|
| flat | 10 | 2.750 | 6.683 | 2,43x |
| flat | 30 | 12.979 | 14.842 | 1,14x |
| flat | 60 | 25.483 | 26.872 | 1,05x |
| nested | 10 | 7.646 | 8.471 | 1,11x |
| nested | 30 | 22.189 | 20.270 | **0,91x** |
| nested | 60 | 43.903 | 37.760 | **0,86x** |
| oneof | 10 | 9.552 | 9.355 | **0,98x** |
| oneof | 30 | 27.075 | 22.014 | **0,81x** |
| oneof | 60 | 53.259 | 40.794 | **0,77x** |

### Bacaan

Perbaikan **monoton di kedua sumbu**, dan itu persis arah dua klaim pertama
blog:

- **lebih banyak tool** -> flat 2,43x -> 1,14x -> 1,05x
- **tool lebih kompleks** -> pada 60 tool: 1,05x -> 0,86x -> 0,77x

Titik silang (code mode jadi lebih murah):

| schema | silang di |
|---|---|
| flat | belum, tapi mendekati 1,0 |
| nested | 30 tool |
| oneof | 10 tool |

Kasus terbaik 60 tool + `oneof`: **23% lebih hemat konteks**.

Penyebabnya jelas: biaya code mode untuk *deklarasi tool* **konstan** (484
char, satu tool), sementara direct mode naik linear terhadap jumlah dan
kompleksitas tool. Blok instruction code mode juga naik, tapi lebih lambat -
`oneOf` + enum besar jadi union TS yang padat, sedangkan di JSON Schema
bentuknya bertele-tele.

Tapi perhatikan juga: **code mode TIDAK otomatis lebih hemat.** Pada katalog
kecil berschema sederhana dia 2,4x lebih mahal. Penghematannya datang dari
skala dan kompleksitas, bukan dari polanya sendiri.

### Pengukuran lama (proxy) - DIGANTIKAN

Versi pertama memakai proxy `len(json.dumps(input_schema)) + description +
name` dan menghasilkan rasio 1,90-2,24x pada flat. Angka itu **meremehkan
direct mode ~2x**: untuk 60 tool flat, proxy menghitung 13.305 char sementara
deklarasi ADK yang sebenarnya 25.471 char.

Peringatan soal ini sudah ditulis sejak awal, dan terbukti benar. Jangan
pakai angka proxy itu.

---

## Temuan: 58% deklarasi awalnya adalah boilerplate

Pengukuran pertama memberi rasio 3,25-3,44x pada schema flat. Pelacakan
penyebabnya:

| tool | dengan envelope | tanpa envelope | overhead | % |
|---:|---:|---:|---:|---:|
| 10 | 7.742 | 3.566 | 4.176 | 54% |
| 30 | 22.041 | 9.565 | 12.476 | 57% |
| 60 | 43.281 | 18.355 | 24.926 | **58%** |

Penyebabnya perbaikan Fase 3a: envelope `CallToolResult` dipancarkan **per
tool**, padahal `content` dan `isError` identik di semua tool.

Kalau dibiarkan, kesimpulan "code mode lebih mahal konteks" sebagian adalah
artefak codegen saya, bukan sifat polanya. Jadi diperbaiki dulu sebelum
menarik kesimpulan.

### Perbaikan: komposisi `allOf` -> `extends`

`Catalog` kini mengeluarkan output schema sebagai komposisi JSON Schema
standar:

```json
{"allOf": [{"$ref": "#/shared/McpResultBase"}, {"properties": {"structuredContent": ...}}]}
```

`Codegen` menerjemahkannya jadi `interface XOutput extends McpResultBase { ... }`
dan memancarkan tipe bersama **sekali**. Batas lapisan tetap terjaga:
pengetahuan MCP ada di `Catalog`, `Codegen` hanya mengenal `allOf` + `$ref`.

Hasil:

| tool | sebelum | sesudah | hemat |
|---:|---:|---:|---:|
| 10 | 7.742 | 5.030 | −35% |
| 30 | 22.041 | 13.189 | −40% |
| 60 | 43.281 | 25.219 | **−42%** |

Overhead envelope turun 58% -> 27%. Sisa 27% adalah tipe
`structuredContent` per tool, yaitu informasi asli, bukan boilerplate.

Server filesystem: 14.211 -> **10.271 char**.

---

## Bagian B — Correctness & token (dengan LLM)

Sumber: `build/eval_gemini-3.5-flash.jsonl`, 32 run.
**Grid belum lengkap** — baru 10 dan 30 tool; 60 tool belum terukur.

> Pengumpulan data dihentikan atas permintaan pemilik proyek: jalur Gemini API
> langsung dimatikan (`ALLOW_GEMINI_DIRECT = False` di `eval/harness.py`).
> Kelanjutannya harus lewat endpoint OpenAI-compatible.

### Correctness: sempurna di kedua mode

| | direct | code |
|---|---|---|
| benar | 16/16 | 16/16 |
| pengecoh terpanggil | 0 | 0 |

Juga dikonfirmasi oleh 48 run terpisah lewat `cc/claude-sonnet-5-5`:
48/48 benar, 0 pengecoh.

**Akurasi pemilihan tool tidak jadi masalah** pada katalog sampai 60 tool.
Jadi klaim blog soal "handle many more tools" tidak terbukti lewat jalur
akurasi — terbuktinya lewat biaya konteks (Bagian A).

### Token: tergantung satu hal, dan bukan jumlah tool

| jumlah tool | akurasi | token direct | token code | selisih | gerbang |
|---:|---|---:|---:|---:|---|
| 10 | 100% → 100% | 4.877 | 4.213 | **−14%** | LOLOS |
| 30 | 100% → 100% | 10.648 | 16.088 | **+51%** | gagal |

Per kategori (median token input):

| kategori | direct | code | turn direct | turn code |
|---|---:|---:|---:|---:|
| single | 5.232 | 7.473 | 2,5 | 2,0 |
| chain | 6.924 | 11.593 | 3,0 | 3,5 |
| data | 7.744 | **30.548** | 3,0 | **7,5** |
| control | 9.263 | **6.268** | 4,0 | **2,0** |

Kategori `data` anjlok 4x. Itu bukan derau — ada penyebabnya.

### Temuan utama: penentunya adalah kode-jadi-sekali

Rincian per task memperlihatkan korelasi yang hampir sempurna:

| kelompok | n | median rasio | median turn |
|---|---:|---:|---:|
| kode jadi sekali (turn=2) | 8 | **0,85x** | 2,0 |
| perlu diperbaiki (turn>2) | 8 | **2,06x** | 4,5 |

Tingkat jadi-sekali: **8/16 (50%)** pada `gemini-3.5-flash`.

Contoh ekstrem, task `d2` ("sebutkan id pesanan dengan items >= 5"):

| | turn | tool call | token input |
|---|---:|---:|---:|
| direct, 10 tool | 3 | 5 | 4.843 |
| code, 10 tool | **12** | **19** | **38.089** (7,86x) |
| code, 30 tool | 10 | 17 | 51.676 (4,89x) |

Jawabannya benar, tapi butuh 12 turn. Mekanismenya: setiap perbaikan kode
mengirim ulang **seluruh blok deklarasi + kode yang gagal + pesan error**.
Direct mode yang gagal cukup mengulang satu tool call.

Jadi kerugian code mode saat gagal **jauh lebih besar** daripada keuntungannya
saat berhasil — dan blog tidak membahas ini sama sekali.

### Implikasi praktis

Pernyataan yang bisa dipertanggungjawabkan dari data ini:

1. **Code mode bukan penghematan tanpa syarat.** Nilai harapannya bergantung
   pada tingkat kode-jadi-sekali. Pada 50%, nilai harapannya negatif.
2. **Metrik yang harus dipantau adalah tingkat jadi-sekali**, bukan jumlah
   tool. Itu metrik yang tidak ada di blog maupun di implementasi lain yang
   diperiksa.
3. **Task single-call selalu kalah** (median 1,43x) — sesuai rancangan. Kalau
   kategori ini menang, pengukurannya patut dicurigai.
4. **Yang paling menguntungkan code mode adalah control-flow** (0,68x): loop
   polling dikerjakan di dalam kode, bukan lewat bolak-balik agent loop. Ini
   sesuai contoh di blog Anthropic.

### Yang akan memperbaiki angkanya

Belum dikerjakan, tapi arahnya jelas dari data:

- Naikkan tingkat jadi-sekali: contoh pemakaian di instruction, pesan error
  yang lebih tajam, atau `deno check` atas kode model sebelum dieksekusi.
- Batasi biaya perbaikan: jangan kirim ulang blok deklarasi penuh di turn
  perbaikan.
- Ukur 60 tool untuk melengkapi sumbu.

### Catatan model

| model | endpoint | catatan |
|---|---|---|
| `gemini-3.5-flash` | Gemini API | **dihentikan** atas permintaan pemilik proyek |
| `ag/gemini-3.8-flash` | proxy OpenAI-compatible | 32 run; **token tidak sahih** (dugaan prompt caching) |
| `cc/claude-sonnet-5-5` | proxy OpenAI-compatible | 48 run; correctness sahih, token tidak sahih |

**Token tidak sebanding lintas endpoint.** Task `s1` dilaporkan 1.574 token
lewat Gemini API langsung dan 7.533 lewat proxy — endpoint menyuntikkan system
prompt ~3,2k token.

### Dugaan prompt caching: belum terverifikasi

Request code mode membawa `system` 26.492 char tapi dilaporkan
`prompt_tokens=2105`. Endpoint itu konsisten ~2,1 char/token untuk teks nyata,
jadi seharusnya ~12.600 token. Selisih 6x paling masuk akal dijelaskan prompt
caching, di mana `prompt_tokens` mengecualikan bagian ter-cache.

Kalau benar, `prompt_tokens` mengukur **biaya tagihan**, bukan **ukuran
konteks** — dan nilainya bergantung urutan eksekusi. `eval/token_proxy.py`
sudah menghitung `request_tokens` sendiri dengan tiktoken untuk kasus ini,
tapi hipotesisnya belum dikonfirmasi lewat field cache di objek `usage`.

