# Laporan F6: apakah performa naik dan efisiensi bertambah?

**Jawaban singkat: tidak. Refactor F6 adalah peningkatan ketahanan, bukan performa.** Pada beban
yang diukur, efisiensi codemode sedikit memburuk. Tidak ada klaim peningkatan performa yang
didukung data.

Pembanding: `main` @ `8d3f8cd` (sebelum) vs `f6-refactor` (sesudah). Model `cc/claude-sonnet-5-5`,
server MCP lifecycle sungguhan, n=10 per sisi, direct sebagai kontrol.

## Efisiensi token (bench x10, tugas list_*, 4 giliran, hasil terbesar 984 char)

| metrik | sebelum | sesudah | selisih |
|---|---:|---:|---:|
| direct token median (kontrol) | 15.094 | 15.098 | +0,03% |
| codemode token median | 18.302 | 18.895 | **+3,2%** |
| codemode input median | 17.670 | 18.154 | +2,7% |
| codemode output median | 626 | 735 | **+17%** |
| codemode biaya berbobot (output x5) | 20.808 | 21.843 | **+5,0%** |
| rasio biaya codemode/direct | 1,20x | 1,26x | lebih buruk |

Penyebab (terukur): PROMPT tumbuh 1.183 -> 1.370 char (360 -> 417 token, +57 x 4 giliran, ~+230
token input). Sisa input dan kenaikan output (+17%) berasal dari model yang kini menulis
`export default`; mekanismenya tidak saya isolasi.
Plafon `ModelBudget` tidak pernah mengikat pada beban ini.

## Latensi dan beban backend

| metrik | sebelum | sesudah |
|---|---:|---:|
| overhead 1 call (sandbox, 3 ronde x 25) | 77-81 ms | 76-79 ms |
| overhead 20 call | 86-90 ms | 85-91 ms |
| wall codemode median (mcpcost x10) | 7.126 ms | 7.488 ms |
| wall direct median (kontrol) | 6.422 ms | 6.189 ms |
| rasio wall | 1,11x | 1,21x |
| call MCP / byte MCP | 1,00x / 1,00x | 1,00x / 1,00x |

- Overhead per call dari refactor (adapter, CallRecord, adk_compat): **tidak terukur**, di dalam
  derau. Itu hasil yang diharapkan dan baik.
- Selisih wall 1,11x -> 1,21x tidak bisa dipisahkan dari derau LLM (direct sendiri bergeser
  -4%). Kemungkinan sebagian dari token output tambahan; tidak terbukti.
- Biaya deklarasi (`decl_cost.py`) identik 0,77x / 1,05x; skrip itu tidak menghitung PROMPT.

## Apa yang memang bertambah (bukan performa)

| | sebelum | sesudah |
|---|---|---|
| test | 108 | 141 (+2 Docker kini jalan) |
| titik keputusan `flat` | 3 tempat | 1 (`Plan`) |
| dialek kegagalan tool | `isError` saja; `{"error"}` lolos jadi nilai sukses | satu fungsi `classify` |
| kanal teks bebas ke model | 1, plafon 100.000 char | semua lewat `ModelBudget`, 4.000 char |
| akuntansi call | direkonstruksi dari callback (bug bersarang) | `CallRecord` dipancarkan |
| perubahan versi ADK | callback bisa dilewati senyap | `AdkIncompatible` saat dipakai |
| diuji di ADK | 1.33.0 | 1.33.0 dan 1.39.1 (141 lolos keduanya) |

## Batas laporan ini

- Satu beban kerja saja (hasil kecil). Manfaat plafon output hanya masuk akal pada hasil besar;
  **belum diukur**. Hasil itu bisa membalik kesimpulan token, bisa juga tidak.
- n=10, model nondeterministik; selisih di bawah ~5% tidak bisa dibedakan dari derau.
- Baseline "sebelum" tidak dijalankan ulang bersamaan dengan "sesudah" (berselang jam); drift
  server/LLM tidak terkendali selain kontrol direct.
- Perilaku agent codemode yang menyerah tanpa mencoba (0 call di compare.py) ada sebelum dan
  sesudah F6; tidak diperbaiki.

Mentah: `docs/baseline/` (`bench10_token*.txt`, `mcpcost10*.txt`).
