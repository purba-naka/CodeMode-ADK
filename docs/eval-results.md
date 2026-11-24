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

Sumber: `build/eval_cc_claude-sonnet-5-5.jsonl` (48 run, grid lengkap) dan
`build/eval_sweep_r*.jsonl` + `build/eval_bigdata_r50.jsonl` (sapuan ukuran
payload).

Model: `cc/claude-sonnet-5-5` lewat endpoint OpenAI-compatible.

### Sumber angka token

**Dihitung sendiri di `eval/token_proxy.py`** dengan tiktoken `o200k_base` atas
body permintaan HTTP yang sebenarnya. Bukan dari `prompt_tokens` provider,
karena angka itu terbukti tidak konsisten:

| diukur | tiktoken | lapor provider |
|---|---:|---:|
| request code mode, 15.704 char | 4.312 | 2.133 (meremehkan ~2x) |
| system prompt 26.000 char | ~7.000 | 15.538 (melebihkan ~2x) |

Dua-duanya tidak bisa benar sekaligus, jadi angka provider tidak dipakai
menyimpulkan apa pun.

**Hipotesis prompt caching: DIUJI dan TERBANTAHKAN.** Prompt identik dikirim
3x berturut melaporkan `prompt_tokens` yang persis sama (15.538), dan prompt
yang diubah pun sama. Tidak ada tanda caching.

### Correctness: sempurna

48/48 benar di kedua mode. **0 pengecoh terpanggil** di semua titik grid
sampai 60 tool. Jadi klaim "handle many more tools" tidak terbukti lewat
jalur akurasi pemilihan tool - akurasinya tidak pernah jadi masalah.

### Tingkat kode-jadi-sekali tergantung model

| model | jadi sekali | catatan |
|---|---|---|
| `gemini-3.5-flash` | 8/16 (50%) | task d2 sampai 12 turn / 19 call |
| `cc/claude-sonnet-5-5` | **24/24 (100%)** | semua turn=2 |

Ini mengubah kesimpulan sebelumnya. Dengan `gemini-3.5-flash`, biaya
perbaikan kode mendominasi dan code mode kalah 11/16. Dengan
`cc/claude-sonnet-5-5`, tidak ada satu pun perbaikan diperlukan.

**Tingkat jadi-sekali adalah variabel paling menentukan, dan blog tidak
membahasnya sama sekali.**

### Grid jumlah tool, dataset KECIL (12 pesanan)

| tool | akurasi | direct | code | selisih |
|---:|---|---:|---:|---:|
| 10 | 100% → 100% | 3.007 | 4.272 | +42% |
| 30 | 100% → 100% | 5.263 | 8.881 | +69% |
| 60 | 100% → 100% | 12.679 | 15.763 | +24% |

Code mode kalah di semua titik. Perhatikan pola yang menjelaskannya:

```
biaya code mode per titik grid:  4.272 / 8.881 / 15.763   <- nyaris DATAR
                                  antar task di titik yang sama
biaya direct mode:                bervariasi ikut jumlah tool call
```

Biaya code mode didominasi blok deklarasi dan **tidak peduli isi task**.
Dengan payload kecil, tidak ada yang bisa disaring - jadi deklarasi itu murni
beban.

### Cacat rancangan task saya, dan perbaikannya

Task "data-heavy" saya cuma 12 pesanan. Itu **tidak berat**, dan artinya eval
saya **tidak pernah menguji mekanisme utama yang diklaim blog**:

> "the output of each tool call must feed into the LLM's neural network, just
> to be copied over to the inputs of the next call, wasting time, energy, and
> tokens"

`eval/mock_server.py --rows N` sekarang bisa memperbesar dataset secara
deterministik, dengan jawaban **tertutup** (tepat 1 dari 4 baris pending,
`total = 10.000 + (i % 50) * 1.000`) sehingga tetap bisa diverifikasi
otomatis.

### Hasil sapuan ukuran payload (task b1, 10 tool)

| rows/wilayah | payload 1 wilayah | direct | code | rasio |
|---:|---:|---:|---:|---:|
| 2 | 692 char | 3.527 | 4.292 | 1,22x |
| 5 | 1.714 char | 6.053 | 4.320 | **0,71x** |
| 15 | 5.135 char | 14.483 | 4.333 | **0,30x** |
| 50 | 17.156 char | 43.947 | 4.388 | **0,10x** |
| 100 | 34.332 char | 86.037 | 4.322 | **0,05x** |

Semua benar di kedua mode.

```
code mode : ~4.300 token, DATAR - tidak peduli payload sebesar apa
direct    : linear terhadap ukuran payload
```

Titik silang: payload per wilayah antara ~700 dan ~1.700 char.

Pada payload 34 KB per wilayah, code mode **95% lebih hemat**. Mekanismenya
persis seperti yang blog gambarkan: data disaring di dalam sandbox, hanya satu
angka yang masuk konteks model.

Diverifikasi juga di task kedua (b2, agregasi nilai, `rows=50`):
direct 45.015 → code 4.422 token, **−90%**.

### Kesimpulan yang bisa dipertanggungjawabkan

| Klaim blog | Hasil |
|---|---|
| "many more tools" | ✅ **sebagian** — hemat di biaya deklarasi (Bagian A, hingga −23%), tapi di percakapan nyata masih +24% pada 60 tool dengan payload kecil. Akurasi pemilihan tool tidak pernah jadi masalah |
| "more complex tools" | ✅ didukung (Bagian A), efek terkuat |
| "shines when stringing together multiple calls" | ✅ **terbukti kuat**, tapi bukan karena jumlah call - karena **ukuran hasil antara**. −90% sampai −95% pada payload besar |

Dua syarat agar code mode menguntungkan:

1. **Kode model jadi di percobaan pertama.** Kalau tidak, tiap perbaikan
   mengirim ulang blok deklarasi + kode gagal + error. Pada
   `gemini-3.5-flash` (50% jadi-sekali) nilai harapannya negatif; pada
   `cc/claude-sonnet-5-5` (100%) tidak jadi masalah.
2. **Ada hasil antara yang layak disaring.** Di bawah ~1-2 KB per tool call,
   blok deklarasi lebih mahal daripada yang dihemat.

Keduanya tidak disebut di blog.



---

## Celah metodologi yang baru ketemu saat dipakai di luar (2026-11-22)

Ditemukan saat memasang `codemode-adk` ke agent ADK sungguhan terhadap server
MCP lifecycle (`codemode-vs-direct`). Tiga hal yang eval ini **tidak** ukur,
dan ketiganya mengubah kesimpulan praktis:

### 1. Token mentah bukan biaya

Seluruh angka di dokumen ini adalah **jumlah token**, input dan output
diperlakukan sama. Harga nyata tidak begitu: output biasanya **3-5x** lebih
mahal dari input.

Terukur pada pertanyaan data-berat, 3 run per mode:

| | direct | code | rasio |
|---|---:|---:|---:|
| token mentah (median) | 16.552 | 16.997 | 1,03x |
| **biaya berbobot** (out x5) | 28.168 | 23.469 | **0,83x** |

Arahnya berbeda. Sebabnya mekanis: direct memakai output 2.409-3.772 token
karena model **menghitung aritmetika dalam prosa**; code mode 1.539-1.641
karena ia menulis kode. Jadi code mode menghemat token **output**, bagian
yang paling mahal - dan eval ini tidak pernah menyorotnya.

### 2. Yang diukur hanya tengah, bukan ekor

| | median | rata-rata | rentang |
|---|---:|---:|---:|
| direct | 16.552 | 47.154 | 16.059 - **108.851** |
| code | 16.997 | 16.901 | 16.418 - 17.289 |

Pada satu dari tiga run, direct menarik **82.662 char** mentah ke konteks -
6,6x median-nya sendiri. Code mode mentok ~17k di ketiga run.

Jadi yang dibeli code mode bukan sekadar rata-rata lebih murah, tapi
**langit-langit**. Itu properti yang berbeda, dan untuk sistem produksi
seringkali lebih berharga daripada rata-rata. Grid 48 run di dokumen ini
melaporkan **jumlah** per titik, jadi properti ini tidak pernah terbaca.

### 3. Kemampuan API tool adalah variabel yang tidak pernah divariasikan

Mock server di `eval/mock_server.py` mengembalikan data apa adanya. Server
sungguhan tidak: `query_table` menyediakan `columns`, `filters`, `limit`, dan
membatasi hasil di `MAX_ROWS=100`. Akibatnya model sering memproyeksikan kolom
sendiri, payload besar **tidak pernah terjadi**, dan code mode tidak punya apa
pun untuk dihemat - sambil tetap membayar ~1.000-1.300 token per giliran untuk
blok deklarasi.

Ini konsisten dengan syarat yang blog Cloudflare sebutkan sendiri:

> "the output of each tool call must feed into the LLM's neural network, just
> to be copied over to the inputs of the next call... When the LLM can write
> code, it can skip all that, and only read back the final results it needs."

Kalau keluaran tool tidak perlu disalin lewat model, tidak ada yang dilewati.
**Semakin pintar API tool-nya, semakin kecil yang dibeli code mode.** Dimensi
ini layak masuk grid kalau eval ini dilanjutkan: sumbu "tool bodoh vs tool
ber-agregasi", bukan hanya jumlah tool dan ukuran payload.

## Baseline sebelum refactor F6 (commit 8d3f8cd)

`docs/baseline/mcpcost10.txt`: 10 run x 2 mode, server MCP sungguhan. Call MCP 1,00x,
byte MCP 1,00x (2314), waktu MCP 0,88x, wall 1,11x. Pertanyaan default `mcpcost.py`
(list_*), bukan `query_table` - jadi rasio byte 2,90x di README codemode-vs-direct
TIDAK direproduksi oleh pertanyaan ini; bedanya pertanyaan, bukan koreksi.

KETERBATASAN: `bench10.txt` hanya berisi "WIRE_LOG belum diset". Angka token butuh proxy
`eval/token_proxy.py` (port 20129) yang tidak dijalankan. Baseline TOKEN belum ada;
wajib diambil sebelum F6-3 (yang mengubah plafon output).

## Catatan metodologis F6 (refactor `f6-refactor`)

Dua celah ditemukan dengan MEMBACA kode orang lain (PINTER_ADK_REFACT), bukan oleh
108 test kita: dialek kegagalan `{"error": ...}` milik ADK, dan desain pencatat call
tanpa kelas bug atribusi bersarang. Gerbang menguji apa yang sudah kita pikirkan;
tidak satu pun akan menemukan dialek yang belum terbayangkan. Karena itu kegagalan
kini lewat satu titik (`adk/outcome.py`), bukan tersebar.

Ditunda: F6-3 (ModelBudget + kanal nilai kembali). Mengubah plafon output = mengubah
semua angka eval, dan baseline token belum ada (lihat bagian baseline di atas).
