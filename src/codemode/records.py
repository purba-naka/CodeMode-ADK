"""CallRecord: observabilitas yang DIPANCARKAN sistem, bukan direkonstruksi.

Kelas bug yang dieliminasi: akuntansi tool call yang bersumber dari callback
ADK. Call MCP di code mode BERSARANG di dalam `execute_code`:
    before(execute_code) > before(query_table) > after(query_table) > after(execute_code)
Pengukur yang menulis ke "entri terakhir" menimpa entri yang salah. Itu
persis yang terjadi: byte MCP code mode tampak 0,59x padahal 2,90x.

ATURAN: akuntansi call/byte/latensi HANYA boleh bersumber dari CallRecord.
Bersarangnya eksplisit di tipe (`parent_call_id`, `depth`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    parent_call_id: str
    depth: int
    server: str
    tool: str
    ok: bool
    #: None kalau ok. Selain itu "mcp_is_error" | "adk_error_dict" | "exception:<Tipe>"
    failure_kind: str | None
    duration_ms: int
    #: ukuran hasil yang dikirim ke sandbox (JSON); 0 kalau gagal
    bytes_out: int
