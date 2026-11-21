"""Task evaluasi - semua jawaban diturunkan dari ORDERS di mock_server.py.

Komposisi mengikuti rencana Fase 4, dan setiap kategori punya ekspektasi
eksplisit tentang siapa yang seharusnya menang. Kategori `single` dirancang
supaya **code mode kalah**; kalau dia menang di sana, pengukurannya salah.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


def _nums(text: str) -> set[int]:
    """Semua angka di teks, pemisah ribuan diabaikan."""
    out: set[int] = set()
    for m in re.finditer(r"\d[\d.,]*", text):
        raw = m.group(0).rstrip(".,")
        for cleaned in {raw.replace(".", "").replace(",", ""), raw.replace(",", "")}:
            if cleaned.isdigit():
                out.add(int(cleaned))
    return out


def has(*values: int) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        return set(values) <= _nums(text)

    return check


def has_words(*words: str) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        low = text.lower()
        return all(w.lower() in low for w in words)

    return check


def both(*checks: Callable[[str], bool]) -> Callable[[str], bool]:
    def check(text: str) -> bool:
        return all(c(text) for c in checks)

    return check


@dataclass(frozen=True)
class Task:
    id: str
    kind: str  # single | chain | data | control
    prompt: str
    check: Callable[[str], bool]
    #: Tool inti yang wajar dipakai. Untuk akurasi pemilihan tool: dipakai
    #: tool inti, atau malah terpancing ke pengecoh?
    expect_tools: set[str] = field(default_factory=set)
    note: str = ""


TASKS: list[Task] = [
    # ---- 2 single-call: ekspektasi CODE MODE KALAH -------------------------
    Task(
        id="s1",
        kind="single",
        prompt="Wilayah apa saja yang punya pesanan? Sebutkan semuanya.",
        check=has_words("jakarta", "bandung", "surabaya", "medan"),
        expect_tools={"list_regions"},
        note="satu call; overhead code mode seharusnya merugikan",
    ),
    Task(
        id="s2",
        kind="single",
        prompt="Berapa nilai total seluruh pesanan di wilayah medan?",
        check=has(205_000),
        expect_tools={"get_region_summary"},
        note="satu call",
    ),
    # ---- 3 multi-call berantai: medan utama -------------------------------
    Task(
        id="c1",
        kind="chain",
        prompt=(
            "Di wilayah jakarta, cari pesanan dengan nilai total paling besar. "
            "Sebutkan id-nya dan berapa jumlah item pesanan itu."
        ),
        check=both(has_words("A7"), has(9)),
        expect_tools={"get_orders_by_region"},
        note="output call 1 jadi input call 2",
    ),
    Task(
        id="c2",
        kind="chain",
        prompt=(
            "Untuk setiap wilayah yang ada, hitung nilai total pesanannya. "
            "Wilayah mana yang totalnya paling besar, dan berapa nilainya?"
        ),
        check=both(has_words("jakarta"), has(859_000)),
        expect_tools={"list_regions", "get_region_summary"},
        note="1 call penemuan + 4 call per wilayah",
    ),
    Task(
        id="c3",
        kind="chain",
        prompt=(
            "Berapa banyak pesanan yang statusnya pending, dihitung dari "
            "seluruh wilayah yang ada?"
        ),
        check=has(6),
        expect_tools={"list_regions", "get_orders_by_region"},
        note="penemuan + fan-out + filter",
    ),
    # ---- 2 data-heavy: penghematan terbesar -------------------------------
    Task(
        id="d1",
        kind="data",
        prompt=(
            "Jumlahkan nilai total dari semua pesanan berstatus pending di "
            "seluruh wilayah. Berapa totalnya?"
        ),
        check=has(1_295_000),
        expect_tools={"list_regions", "get_orders_by_region"},
        note="12 pesanan masuk, satu angka keluar",
    ),
    Task(
        id="d2",
        kind="data",
        prompt=(
            "Sebutkan id semua pesanan yang jumlah itemnya 5 atau lebih, "
            "dari seluruh wilayah, urut naik berdasarkan id."
        ),
        check=has_words("A3", "A5", "A7", "B1"),
        expect_tools={"list_regions", "get_orders_by_region"},
        note="filter + sortir di dalam kode",
    ),
    # ---- 1 control-flow ---------------------------------------------------
    Task(
        id="k1",
        kind="control",
        prompt=(
            "Cek status deploy berulang kali sampai statusnya 'complete'. "
            "Berapa kali percobaan yang dibutuhkan sampai complete?"
        ),
        check=has(3),
        expect_tools={"poll_deploy_status"},
        note="loop dengan kondisi berhenti; 'complete' di polling ke-3",
    ),
]

BY_ID = {t.id: t for t in TASKS}
