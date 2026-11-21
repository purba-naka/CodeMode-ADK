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

Deterministik, instan. Mengukur biaya **definisi tool**, bukan hasil antara.

| tool | schema | direct (char) | code (char) | rasio |
|---:|:---|---:|---:|---:|
| 10 | flat | 2.248 | 5.030 | 2,24x |
| 30 | flat | 6.711 | 13.189 | 1,97x |
| 60 | flat | 13.305 | 25.219 | 1,90x |
| 10 | nested | 5.318 | 6.818 | 1,28x |
| 30 | nested | 15.921 | 18.617 | 1,17x |
| 60 | nested | 31.725 | 36.107 | 1,14x |
| 10 | oneof | 7.224 | 7.702 | 1,07x |
| 30 | oneof | 20.807 | 20.361 | **0,98x** |
| 60 | oneof | 41.081 | 39.141 | **0,95x** |

### Bacaan

**Code mode TIDAK otomatis lebih hemat konteks.** Pada schema sederhana,
deklarasi TS justru ~2x lebih besar dari definisi tool langsung.

Tapi arah trennya konsisten dan monoton di kedua sumbu, dan ada **titik
silang**: pada schema `oneof` dengan >=30 tool, code mode menjadi lebih kecil.
Alasannya schema kompleks membengkakkan direct mode lebih cepat daripada
membengkakkan TS - `oneOf` + enum besar jadi union TS yang padat.

Ini mendukung arah klaim "more complex tools", tapi **membantah** pembacaan
naif bahwa code mode selalu memangkas konteks. Penghematan utamanya harus
datang dari **hasil antara** (klaim 3), bukan dari definisi tool.

### Peringatan metodologi

Angka `direct` adalah **proxy**: `len(json.dumps(input_schema)) + description +
name` per tool. Definisi tool yang sebenarnya dikirim ke Gemini punya
pembungkus tambahan, jadi proxy ini kemungkinan **meremehkan** direct mode.
Angka ground-truth ada di Bagian B lewat `usage_metadata`.

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

Lihat `build/eval_toolcount.json`. Diisi setelah run selesai.

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
