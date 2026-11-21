"""Proxy pencatat token - ground truth dari kabel, bukan dari pembukuan ADK.

Kenapa ini ada: `usage_metadata` event ADK lewat `LiteLlm` terbukti SALAH.
Diukur 2026-11-21 dengan `cc/claude-sonnet-5-5` lewat endpoint
OpenAI-compatible:

    ADK melaporkan  : 4107 token input, SAMA untuk 10/30/60 tool
                      dan SAMA untuk direct vs code mode
    kabel melaporkan: 6575 / 10515 / 16425 untuk 10/30/60 tool

Endpoint-nya benar - tool dan system prompt dihitung dengan semestinya
(system 25.000 char -> +25.000 token). Yang salah adalah apa yang sampai ke
`usage_metadata`.

Karena seluruh Fase 4 bergantung pada angka token, angka itu harus diambil
dari lalu lintas HTTP yang sebenarnya.

Pakai:
    python eval/token_proxy.py --port 20129 --upstream http://localhost:20128/v1
    # lalu arahkan LITELLM_BASE_URL ke http://localhost:20129/v1
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_LOCK = threading.Lock()

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:
    _ENC = None


def _count_tokens(body: bytes | None) -> int:
    """Hitung token permintaan sendiri, dengan tokenizer yang konsisten.

    Angka dari provider tidak bisa dipakai membandingkan ukuran prompt kalau
    ada prompt caching: `prompt_tokens` mengecualikan bagian yang ter-cache,
    jadi besarannya bergantung pada urutan eksekusi, bukan pada isi prompt.

    Ini bukan hitungan tokenizer model yang sebenarnya, tapi konsisten dan
    sebanding antar mode - dan itulah yang dibutuhkan eval.
    """
    if not body:
        return 0
    text = body.decode("utf-8", "replace")
    if _ENC is None:
        return len(text) // 4
    return len(_ENC.encode(text, disallowed_special=()))


class Handler(BaseHTTPRequestHandler):
    upstream: str = ""
    out_path: Path = Path("build/wire_usage.jsonl")
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass  # jangan kotori stdout harness

    # ------------------------------------------------------------- helpers
    def _target(self) -> str:
        base = self.upstream.rstrip("/")
        path = self.path
        # kedua sisi bisa membawa /v1; jangan sampai ganda
        if base.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        return base + path

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in {"host", "content-length", "connection"}
        }

        n_tools = 0
        model = ""
        tool_names: list[str] = []
        sys_chars = 0
        self._req_chars = len(body or b"")
        self._req_tokens = _count_tokens(body)
        if body:
            try:
                parsed = json.loads(body)
                tools = parsed.get("tools") or []
                n_tools = len(tools)
                model = parsed.get("model", "")
                tool_names = [
                    (x.get("function") or {}).get("name", "?") for x in tools
                ]
                sys_chars = sum(
                    len(m.get("content") or "")
                    for m in (parsed.get("messages") or [])
                    if m.get("role") == "system"
                )
            except (json.JSONDecodeError, AttributeError):
                pass

        req = urllib.request.Request(
            self._target(), data=body, headers=headers, method=method
        )

        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            ctype = e.headers.get("Content-Type", "text/plain")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except Exception as e:
            payload = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        with resp:
            ctype = resp.headers.get("Content-Type", "")
            is_sse = "event-stream" in ctype

            def copy_headers() -> None:
                for k, v in resp.headers.items():
                    if k.lower() in {
                        "transfer-encoding",
                        "content-length",
                        "connection",
                    }:
                        continue
                    self.send_header(k, v)

            usage: dict | None = None

            if is_sse:
                self.send_response(resp.status)
                copy_headers()
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                buf = b""
                for chunk in resp:
                    self._write_chunk(chunk)
                    buf += chunk
                    # Ambil usage dari potongan SSE TERAKHIR yang punya
                    # `usage`; potongan awal kadang membawa hitungan parsial.
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        s = line.decode("utf-8", "replace").strip()
                        if not s.startswith("data:"):
                            continue
                        data = s[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            d = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(d, dict) and d.get("usage"):
                            usage = d["usage"]
                self._write_chunk(b"")
            else:
                # ADK LiteLlm memanggil dengan `stream=False`, jadi jalur ini
                # yang sebenarnya dipakai: respons JSON biasa, `usage` ada di
                # body. Versi pertama proxy hanya mem-parse SSE dan mencatat
                # NOL request - persis jenis kegagalan senyap yang bikin
                # pengukuran tampak jalan padahal kosong.
                body_out = resp.read()
                self.send_response(resp.status)
                copy_headers()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                self.wfile.write(body_out)
                try:
                    d = json.loads(body_out)
                    if isinstance(d, dict) and isinstance(d.get("usage"), dict):
                        usage = d["usage"]
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        if usage:
            self._record(model, n_tools, usage, tool_names, sys_chars)

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode())
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _record(
        self,
        model: str,
        n_tools: int,
        usage: dict,
        tool_names: list[str] | None = None,
        sys_chars: int = 0,
    ) -> None:
        row = {
            "model": model,
            "n_tools_declared": n_tools,
            "tool_names": (tool_names or [])[:5],
            "system_chars": sys_chars,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            # Objek usage UTUH: endpoint bisa memakai prompt caching, dan
            # `prompt_tokens` lalu MENGECUALIKAN token yang ter-cache. Tanpa
            # field ini, direct vs code mode tampak sama besar padahal
            # prompt-nya beda belasan ribu token.
            "usage_raw": usage,
            # Ukuran permintaan yang dihitung sendiri - deterministik dan
            # tidak terpengaruh cache maupun kebijakan billing provider.
            "request_chars": self._req_chars,
            "request_tokens": self._req_tokens,
        }
        with _LOCK:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    # --------------------------------------------------------------- verbs
    def do_POST(self) -> None:
        self._forward("POST")

    def do_GET(self) -> None:
        self._forward("GET")


def read_usage(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20129)
    ap.add_argument("--upstream", default="http://localhost:20128/v1")
    ap.add_argument("--out", default="build/wire_usage.jsonl")
    ns = ap.parse_args()

    Handler.upstream = ns.upstream
    Handler.out_path = Path(ns.out)

    # Windows mengizinkan dua proses bind ke port yang sama lewat
    # SO_REUSEADDR, dan request jatuh ke salah satunya secara acak. Itu sudah
    # pernah bikin proxy lama diam-diam melayani request dan pencatatan
    # token kosong tanpa error. Jadi tolak kalau port sudah dipakai.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", ns.port), Handler)
    except OSError as e:
        raise SystemExit(
            f"port {ns.port} sudah dipakai ({e}). Matikan proxy lama dulu."
        ) from e
    print(f"proxy token: 127.0.0.1:{ns.port} -> {ns.upstream} | catat ke {ns.out}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
