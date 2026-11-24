"""ExecuteCodeTool - satu-satunya tool yang dilihat model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from codemode.adk.code_mode import code_tool_declaration

if TYPE_CHECKING:
    from codemode.adk.code_mode import CodeMode


class ExecuteCodeTool(BaseTool):
    """Didaftarkan sebagai tool biasa, BUKAN `code_executor=`.

    `code_executor=` memicu batasan one-tool-per-agent ADK dan, pada beberapa
    versi Gemini, `MALFORMED_FUNCTION_CALL` karena function calling dimatikan
    dengan nol tool terdeklarasi.
    """

    def __init__(self, code_mode: CodeMode) -> None:
        super().__init__(
            name="execute_code",
            description=(
                "Jalankan TypeScript di sandbox untuk memanggil tool. "
                "Hanya keluaran console.log yang dikembalikan."
            ),
        )
        self._cm = code_mode

    def _get_declaration(self) -> types.FunctionDeclaration:
        return code_tool_declaration()

    async def run_async(
        self, *, args: dict[str, Any], tool_context: Context
    ) -> dict[str, Any]:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return {
                "status": "error",
                "error": "argumen `code` wajib berupa TypeScript tak kosong",
            }

        result = await self._cm.execute(code, tool_context)

        # SATU-SATUNYA pintu teks bebas ke model (codemode/budget.py).
        shown = self._cm.budget.allocate(result.output, result.value_json)
        payload: dict[str, Any] = {
            "status": result.status,
            "tool_calls": result.calls,
            "output": shown["output"],
        }
        if "result" in shown:
            payload["result"] = shown["result"]
        if "cut" in shown:
            payload["cut"] = shown["cut"]
            # Yang dipotong dari model tidak dibuang: simpan utuh sebagai artifact.
            full = result.overflow or ""
            if "output" in shown["cut"]:
                full = result.output + (chr(10) + full if full else "")
            result.overflow = full or None
        if result.error:
            payload["error"] = result.error
        if result.truncated:
            payload["truncated"] = True
        if result.protocol_violations:
            payload["protocol_violations"] = len(result.protocol_violations)

        # Keluaran berlebih disimpan sebagai artifact, bukan dibuang - supaya
        # host bisa meneruskannya tanpa mengotori konteks model.
        if result.overflow:
            try:
                name = f"codemode_overflow_{result.duration_ms:.0f}.txt"
                await tool_context.save_artifact(
                    name,
                    types.Part.from_bytes(
                        data=result.overflow.encode("utf-8"),
                        mime_type="text/plain",
                    ),
                )
                payload["overflow_artifact"] = name
            except Exception as e:
                payload["overflow_artifact_error"] = str(e)[:200]

        return payload
