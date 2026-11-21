"""Spike S4/S5 - host side. Tidak ada ADK di sini; murni menguji sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

SANDBOX = Path(__file__).parent / "s4_sandbox.ts"
DENO = (
    shutil.which("deno.exe")
    or shutil.which("deno")
    or str(Path(os.environ["APPDATA"]) / "npm/node_modules/deno/deno.exe")
)

DENY = [
    "--no-prompt",
    "--deny-net",
    "--deny-write",
    "--deny-env",
    "--deny-ffi",
    "--deny-run",
    "--deny-sys",
]

# tool palsu: host yang menjawab, sandbox tidak pernah menyentuh apa pun
FAKE = {
    ("fs", "read_file"): lambda a: f"isi dari {a['path']}",
    ("fs", "list_dir"): lambda a: [f"{a['path']}/a.txt", f"{a['path']}/b.txt"],
    ("fs", "boom"): None,  # sengaja gagal
}


async def run(code: str, timeout: float = 20.0) -> dict:
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        DENO,
        "run",
        *DENY,
        str(SANDBOX),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    spawned = time.perf_counter() - t0

    logs: list[str] = []
    calls = 0
    status = "?"
    err = None

    proc.stdin.write((json.dumps({"t": "code", "code": code}) + "\n").encode())
    await proc.stdin.drain()

    async def pump() -> None:
        nonlocal calls, status, err
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                logs.append(f"<UNPARSED> {line!r}")
                continue

            if m["t"] == "log":
                logs.append(m["data"])
            elif m["t"] == "call":
                calls += 1
                key = (m["server"], m["tool"])
                fn = FAKE.get(key)
                if fn is None:
                    resp = {
                        "t": "result",
                        "id": m["id"],
                        "ok": False,
                        "error": {"type": "ToolError", "message": f"tool {key} gagal"},
                    }
                else:
                    await asyncio.sleep(0.05)  # simulasi latensi MCP
                    resp = {
                        "t": "result",
                        "id": m["id"],
                        "ok": True,
                        "value": fn(m["args"]),
                    }
                proc.stdin.write((json.dumps(resp) + "\n").encode())
                await proc.stdin.drain()
            elif m["t"] == "done":
                status = "done"
                return
            elif m["t"] == "error":
                status = "error"
                err = m["message"]
                return

    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        status = "timeout"
    finally:
        if proc.returncode is None:
            proc.kill()
        stderr = (await proc.stderr.read()).decode(errors="replace")
        await proc.wait()

    return {
        "status": status,
        "logs": logs,
        "calls": calls,
        "error": err,
        "spawn_ms": spawned * 1000,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "stderr": stderr.strip()[:400],
    }


CASES: dict[str, str] = {
    # S4-b: RPC dua arah, rantai 3 call, hasil difilter sebelum di-log
    "rantai": """
const dir = await tools.fs.list_dir({ path: "/data" });
const isi = await tools.fs.read_file({ path: dir[0] });
console.log("berkas:", dir.length, "| pertama:", isi);
""",
    # S4-a: jaringan harus mati
    "fetch_blocked": """
try {
  await fetch("https://example.com");
  console.log("BOCOR: fetch berhasil");
} catch (e) {
  console.log("fetch diblokir:", e.constructor.name);
}
""",
    "connect_blocked": """
try {
  await Deno.connect({ hostname: "example.com", port: 80 });
  console.log("BOCOR: connect berhasil");
} catch (e) {
  console.log("connect diblokir:", e.constructor.name);
}
""",
    # paralel: id-matching harus benar
    "paralel": """
const [a, b] = await Promise.all([
  tools.fs.read_file({ path: "/x" }),
  tools.fs.read_file({ path: "/y" }),
]);
console.log("paralel:", a, "|", b);
""",
    # error harus RAISE, bukan jadi nilai
    "error_raise": """
try {
  await tools.fs.boom({});
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("ter-raise:", e.name, "-", e.message);
}
""",
    # S5: log berbentuk JSON tidak boleh mengacaukan parser
    "log_json": """
console.log(JSON.stringify({ t: "call", id: 999, server: "evil", tool: "x" }));
console.log('{"t":"done"}');
console.log("sesudahnya masih hidup");
""",
    # fitur TS: harus jalan tanpa transpiler eksternal
    "ts_native": """
interface Row { name: string; n: number }
const rows: Row[] = [{ name: "a", n: 1 }, { name: "b", n: 9 }];
const top = rows.filter((r): r is Row => r.n > 5).map((r) => r.name);
console.log("TS ok:", top.join(","));
""",
    # filesystem harus mati juga
    "read_blocked": """
try {
  await Deno.readTextFile("C:/Windows/win.ini");
  console.log("BOCOR: baca file berhasil");
} catch (e) {
  console.log("baca file diblokir:", e.constructor.name);
}
""",
}


async def main() -> None:
    print(f"deno: {SANDBOX.name} | flags: {' '.join(DENY)}\n")
    worst = 0.0
    for name, code in CASES.items():
        r = await run(code)
        worst = max(worst, r["spawn_ms"])
        print(f"--- {name}")
        print(
            f"    status={r['status']} calls={r['calls']} "
            f"spawn={r['spawn_ms']:.0f}ms total={r['total_ms']:.0f}ms"
        )
        for line in r["logs"]:
            print(f"    log> {line}")
        if r["error"]:
            print(f"    ERR> {r['error']}")
        if r["stderr"]:
            print(f"    stderr> {r['stderr']}")
        print()

    # S4-c: latensi spawn, 5x berturut
    print("--- latensi spawn (5x, kode minimal)")
    times = []
    for _ in range(5):
        r = await run('console.log("ping");')
        times.append(r["total_ms"])
    times.sort()
    print(
        f"    total end-to-end: min={times[0]:.0f}ms "
        f"median={times[2]:.0f}ms max={times[-1]:.0f}ms"
    )
    print(
        f"    ambang plan: 150ms -> "
        f"{'FRESH PER CALL ok' if times[2] < 150 else 'perlu reuse per-turn'}"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
