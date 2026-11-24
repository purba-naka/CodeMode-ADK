"""ModelBudget: SATU jatah untuk SEMUA kanal yang bisa pulang ke model.

Kelas bug yang dieliminasi: tiap kanal punya aturan sendiri (di PINTER tiga
kanal, satu praktis tak terbatas). Seluruh premis Code Mode adalah menjaga data
di luar konteks; satu kanal longgar membatalkannya.

ATURAN: teks bebas yang dilihat model HANYA boleh lewat `allocate()`.
Menambah kanal baru = tambah ke CHANNELS, dan ia ikut menarik dari jatah total.
Hint pemotongan dihitung DALAM jatah (len(hasil) <= plafon, selalu).
"""

from __future__ import annotations

from dataclasses import dataclass

CHANNELS = ("output", "result")

LOG_HINT = (
    "\n[log dipotong: {cut} dari {full} char. Jangan console.log hasil utuh - "
    "hitung di kode lalu kembalikan hasil kecil lewat `export default`.]"
)
VALUE_HINT = (
    '\n["result" dipotong: {cut} dari {full} char. Ringkas di kode sebelum '
    "dikembalikan.]"
)


def _fit(text: str, limit: int, hint: str) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    note = hint.format(cut=limit, full=len(text))
    if len(note) >= limit:
        return note[:limit], True
    return text[: limit - len(note)] + note, True


@dataclass(frozen=True)
class ModelBudget:
    #: Jatah GABUNGAN semua kanal.
    total_chars: int = 4_000
    #: Plafon tiap kanal; nilai kembali diutamakan (itu kanal yang dianjurkan).
    value_chars: int = 3_000
    log_chars: int = 2_000

    def allocate(self, log: str, value_json: str | None) -> dict[str, str]:
        """Kembalikan {"output": ..., "result": ...(opsional), "cut": ...}.

        Selalu: len(output) + len(result) <= total_chars.
        """
        result = None
        value_cut = False
        used = 0
        if value_json is not None:
            result, value_cut = _fit(
                value_json, min(self.value_chars, self.total_chars), VALUE_HINT
            )
            used = len(result)
        log_room = max(0, min(self.log_chars, self.total_chars - used))
        output, log_cut = _fit(log, log_room, LOG_HINT)
        out = {"output": output}
        if result is not None:
            out["result"] = result
        if log_cut or value_cut:
            out["cut"] = ",".join(
                c for c, f in (("output", log_cut), ("result", value_cut)) if f
            )
        return out
