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

## Bagian B — Correctness & pemilihan tool (dengan LLM)

### Komposisi task

8 task, semua jawabannya diturunkan dari data di `eval/mock_server.py` jadi
bisa diverifikasi otomatis.

| id | kategori | ekspektasi |
|---|---|---|
| s1, s2 | single-call | **code mode harus KALAH** — kalau menang, pengukurannya salah |
| c1, c2, c3 | multi-call berantai | medan utama klaim 3 |
| d1, d2 | data-heavy | penghematan hasil antara terbesar |
| k1 | control-flow | loop dengan kondisi berhenti (complete di polling ke-3) |

### Metrik

| metrik | sumber |
|---|---|
| token input/output | `usage_metadata` event ADK |
| **akurasi pemilihan tool** | pengecoh yang terpanggil, lewat `before_tool_callback` |
| jumlah turn model | hitung event ber-`usage_metadata` |
| tool call sebenarnya | `before_tool_callback` (code mode: lewat `RpcBridge`) |
| correctness | checker per task atas jawaban akhir |

Satu hal yang rapi: **metrik tool call memakai jalur yang sama di kedua mode**
(`before_tool_callback`), karena `RpcBridge` mereplikasi rantai callback ADK.
Itu hasil kerja S2b.

---

## Hasil awal (subset validasi, 10 tool, flat)

| task | mode | benar | token in | out | turn | call |
|---|---|---|---:|---:|---:|---:|
| s1 | direct | OK | 1.591 | 38 | 2 | 1 |
| s1 | code | OK | 8.718 | 220 | 3 | 5 |
| c1 | direct | OK | 2.759 | 54 | 2 | 1 |
| c1 | code | OK | 8.885 | 191 | 3 | 2 |

Pada 10 tool, code mode memakai **~4x token input** — sesuai dugaan, overhead
deklarasi mendominasi saat tool sedikit. Inilah gunanya kategori `single`:
kalau code mode menang di sana, pengukurannya patut dicurigai.

Catatan: angka ini dari sebelum dedup envelope. Angka final ada di Bagian B.

---

## Bagian B — TERTUNDA: pengukuran token lewat endpoint ini belum sahih

Status per 2026-11-21: harness jalan, tapi **angka tokennya belum bisa
dipercaya**. Dicatat apa adanya, termasuk kesalahan yang saya buat sendiri.

### Yang sudah pasti benar

- Harness mendukung `LiteLlm` ke endpoint OpenAI-compatible
  (`LITELLM_BASE_URL` di `.env`, yang di-gitignore).
- Hasil ditulis ke JSONL **per-run** dengan `--resume`. Versi pertama hanya
  menulis di akhir, jadi ketika proses mati di tengah, ~5 menit panggilan LLM
  hilang seluruhnya.
- Satu berkas JSONL per model, dan `model`/`endpoint` dicatat di tiap record.
  Token tidak sebanding lintas model: `gemini-3.5-flash` langsung melaporkan
  1.574 token untuk task s1, sementara lewat proxy `ag/gemini-3.8-flash`
  melaporkan 7.533 untuk task yang sama - endpoint menyuntikkan system prompt
  ~3,2k token.

### Correctness: 100% di kedua mode

Dari 48 run `cc/claude-sonnet-5-5` pada grid 10/30/60 tool: **semua benar**,
dua mode, dan **nol pengecoh terpanggil**. Itu hasil yang sahih - tidak
bergantung pada hitungan token.

Jumlah turn juga sahih dan konsisten: code mode hampir selalu **2 turn**
(satu tulis kode + satu jawab), sementara direct mode naik bersama jumlah
call (c2: 3 turn/9 call, k1: 4 turn/3 call).

### Yang TIDAK sahih: angka token

Tiga hal ditemukan, berurutan:

**1. Saya salah menuduh `usage_metadata` ADK.** Awalnya saya simpulkan ADK
salah lapor karena token input identik untuk 10/30/60 tool. Setelah diukur
dari kabel lewat `eval/token_proxy.py`, ternyata **angka ADK persis sama
dengan kabel** (2.105 + 2.002 = 4.107, tepat seperti laporan ADK). Pembukuan
ADK benar.

**2. Endpoint kemungkinan memakai prompt caching.** Request code mode membawa
`system` 26.492 char, tapi dilaporkan hanya `prompt_tokens=2105`. Untuk teks
nyata endpoint ini konsisten di ~2,1 char/token, jadi 26.492 char seharusnya
~12.600 token. Selisih 6x itu paling masuk akal dijelaskan oleh prompt
caching, di mana `prompt_tokens` mengecualikan bagian yang ter-cache.

Kalau benar, `prompt_tokens` mengukur **biaya tagihan**, bukan **ukuran
konteks** - dan nilainya bergantung pada urutan eksekusi (run pertama bayar
penuh, sisanya tidak). Perbandingan direct vs code mode jadi tidak sahih
kecuali keadaan cache dikendalikan.

**Hipotesis ini BELUM terverifikasi.** Rencananya menyimpan objek `usage`
utuh (mencari field `cache_read_input_tokens`), tapi belum terlaksana.

**3. ADK hanya mendeklarasikan 1 tool ke model.** Catatan kabel menunjukkan
`n_tools_declared: 1` (`execute_code`) untuk code mode - itu memang benar.
Tapi perlu diverifikasi berapa yang dideklarasikan di **direct** mode;
diagnostik saya menimpa berkasnya sebelum terbaca (`_reset_wire()` memotong
per-run, jadi yang tersisa hanya run terakhir). Kalau direct mode juga hanya
mengirim sedikit tool, **seluruh sumbu jumlah-tool tidak mengukur apa pun**.

### Kesalahan saya yang merusak

Saat membersihkan proxy, pola `taskkill` saya `":2012[89].*LISTENING"` ikut
mencocokkan port **20128** - yaitu layanan LiteLLM milik pengguna, bukan hanya
proxy saya di 20129. **Layanan itu saya matikan.** Pola yang benar mencocokkan
port secara tepat:

```bash
netstat -ano | awk '$2 ~ /:20129$/ && $4=="LISTENING" {print $5}'
```

Pengukuran Bagian B berhenti di sini sampai layanan itu hidup lagi.

### Langkah berikutnya, berurutan

1. Hidupkan kembali layanan di port 20128.
2. Simpan objek `usage` utuh di proxy; cari field cache. Kalau ada,
   gunakan `request_tokens` (dihitung sendiri dengan tiktoken di proxy)
   sebagai metrik utama - deterministik dan tidak terpengaruh cache.
3. Verifikasi berapa tool yang benar-benar dideklarasikan di direct mode.
   Kalau hanya 1, sumbu jumlah-tool harus dirancang ulang.
4. Baru jalankan grid penuh dan isi tabel hasil.

Sampai ketiganya selesai, **jangan kutip angka token dari dokumen ini.**
