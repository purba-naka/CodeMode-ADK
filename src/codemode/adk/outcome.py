"""Klasifikasi hasil tool: SATU tempat yang memutuskan "ini kegagalan".

Kelas bug yang dieliminasi: kegagalan disampaikan sebagai nilai di sebagian
jalur dan sebagai exception di jalur lain. Kode sandbox membaca return value,
jadi kegagalan yang lolos sebagai nilai membuat kode lanjut seolah berhasil.

Dialek kegagalan baru = tambah satu cabang di `classify` + satu test.
Tidak ada tempat lain yang boleh memeriksa `isError`/`error` pada hasil tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FailureKind = Literal["mcp_is_error", "adk_error_dict"]


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    message: str


def _mcp_text(result: dict[str, Any]) -> str:
    blocks = result.get("content")
    if isinstance(blocks, list):
        texts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        if texts:
            return " ".join(texts)[:1000]
    return str(result)[:1000]


def classify(result: Any, *, from_tool: bool = True) -> Failure | None:
    """None = sukses. Failure = hasil ini harus menjadi exception di sandbox.

    `from_tool=False` berarti nilai berasal dari callback (mis. deny-rule
    before_tool yang membalas {"error": "ditolak"}). Itu keputusan kebijakan
    yang sengaja dikirim sebagai nilai, jadi dialek `error` tidak berlaku.
    `isError` MCP tetap berlaku untuk keduanya (alasan di bridge._run).
    """
    if not isinstance(result, dict):
        return None
    if result.get("isError"):
        return Failure("mcp_is_error", _mcp_text(result))
    # ADK FunctionTool mengembalikan {"error": ...} alih-alih melempar untuk
    # argumen buruk. Heuristik sempit: hanya kunci error (+status), supaya
    # data sah yang kebetulan punya field `error` di antara field lain aman.
    if (
        from_tool
        and isinstance(result.get("error"), str)
        and set(result) <= {"error", "status"}
    ):
        return Failure("adk_error_dict", result["error"][:1000])
    return None
