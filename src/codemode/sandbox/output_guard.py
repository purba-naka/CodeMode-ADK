"""OutputGuard - membatasi apa yang boleh masuk konteks model.

Lapisan ini tidak tahu soal sandbox maupun ADK.

Alasannya bukan kerapian. Seluruh premis Code Mode adalah "hanya baca hasil
akhir yang dibutuhkan"; kalau log tak dibatasi, satu `console.log` atas hasil
10.000 baris mengembalikan masalah yang justru mau diselesaikan.

Pembandingnya di harness lain: Codex memotong 56.619 token jadi 11.244; Claude
Code memperingatkan di 10k token dan menyimpan ke file di atas 25k.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Default dipilih supaya sebanding dengan harness lain: ~25k token ~ 100k char.
DEFAULT_MAX_TOTAL = 100_000
DEFAULT_MAX_LINE = 8_000
DEFAULT_MAX_LINES = 2_000


@dataclass
class OutputGuard:
    """Mengumpulkan log sambil menegakkan tiga batas sekaligus.

    Satu batas saja tidak cukup:
      - `max_line`  : satu log raksasa
      - `max_lines` : loop yang me-log tiap iterasi
      - `max_total` : akumulasi yang pelan-pelan membengkak
    """

    max_total: int = DEFAULT_MAX_TOTAL
    max_line: int = DEFAULT_MAX_LINE
    max_lines: int = DEFAULT_MAX_LINES

    lines: list[str] = field(default_factory=list)
    total: int = 0
    dropped_lines: int = 0
    truncated_lines: int = 0
    overflow: list[str] = field(default_factory=list)
    _full_total: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.dropped_lines or self.truncated_lines)

    def add(self, text: str, *, stream: str = "log") -> None:
        prefix = "" if stream == "log" else f"[{stream}] "
        self._full_total += len(text)

        if len(self.lines) >= self.max_lines or self.total >= self.max_total:
            self.dropped_lines += 1
            self.overflow.append(prefix + text)
            return

        line = text
        if len(line) > self.max_line:
            line = (
                line[: self.max_line]
                + f"... [{len(text) - self.max_line} char dipotong]"
            )
            self.truncated_lines += 1
            self.overflow.append(prefix + text)

        room = self.max_total - self.total
        if len(line) > room:
            line = line[:room] + "... [batas total tercapai]"
            self.dropped_lines += 0

        self.lines.append(prefix + line)
        self.total += len(line)

    def render(self) -> str:
        """Teks yang dilihat model."""
        body = "\n".join(self.lines)
        if not self.truncated:
            return body
        notes = []
        if self.truncated_lines:
            notes.append(f"{self.truncated_lines} baris dipotong")
        if self.dropped_lines:
            notes.append(f"{self.dropped_lines} baris dibuang")
        tail = (
            f"\n\n[keluaran dibatasi: {', '.join(notes)}. "
            f"Total asli {self._full_total} char. "
            f"Filter di dalam kode, jangan me-log semuanya.]"
        )
        return body + tail

    def overflow_text(self) -> str | None:
        """Keluaran utuh, untuk disimpan sebagai artifact oleh pemanggil."""
        return "\n".join(self.overflow) if self.overflow else None
