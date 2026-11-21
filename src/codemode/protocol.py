"""Protokol stdio antara host dan sandbox: JSON Lines, pesan bertipe.

Semua pesan DARI sandbox divalidasi. Sandbox menjalankan kode yang ditulis
model, jadi keluarannya diperlakukan sebagai masukan tak terpercaya - termasuk
pesan protokolnya sendiri.

Arah host -> sandbox : init, result
Arah sandbox -> host : call, log, done, error
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_CODE_CHARS = 200_000


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------- host -> sandbox
class Init(_Msg):
    """Pesan pertama: manifest tool + batas. Sandbox membangun objek `tools`
    dari manifest ini, bukan dari Proxy tangkap-semua - supaya nama tool yang
    salah gagal langsung dengan TypeError, bukan menggantung di RPC."""

    t: Literal["init"] = "init"
    flat: bool
    manifest: dict[str, list[str]]  # server -> daftar nama tool
    max_calls: int
    code: str = Field(max_length=MAX_CODE_CHARS)


class ToolErrorPayload(_Msg):
    type: str = "ToolError"
    message: str
    server: str | None = None
    tool: str | None = None
    status: int | None = None
    body: str | None = None


class Result(_Msg):
    t: Literal["result"] = "result"
    id: int
    ok: bool
    value: Any = None
    error: ToolErrorPayload | None = None


# --------------------------------------------------------- sandbox -> host
class Call(_Msg):
    t: Literal["call"] = "call"
    id: int = Field(ge=1)
    server: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)


class Log(_Msg):
    t: Literal["log"] = "log"
    stream: Literal["log", "error", "warn", "info", "debug"] = "log"
    data: str


class Done(_Msg):
    t: Literal["done"] = "done"


class Failed(_Msg):
    t: Literal["error"] = "error"
    message: str
    stack: str | None = None


_INBOUND: dict[str, type[_Msg]] = {
    "call": Call,
    "log": Log,
    "done": Done,
    "error": Failed,
}


class ProtocolError(Exception):
    """Pesan dari sandbox tidak bisa dipercaya."""


def parse_inbound(line: str) -> Call | Log | Done | Failed:
    """Parse + validasi satu baris dari sandbox."""
    import json

    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError(f"baris bukan JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ProtocolError(f"pesan bukan objek: {type(raw).__name__}")
    kind = raw.get("t")
    model = _INBOUND.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ProtocolError(f"tipe pesan tak dikenal: {kind!r}")
    try:
        return model.model_validate(raw)  # type: ignore[return-value]
    except Exception as e:
        raise ProtocolError(f"pesan {kind} tidak valid: {e}") from e
