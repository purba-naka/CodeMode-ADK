// Spike S4/S5 — sandbox side.
// Protokol stdio JSON Lines bertipe: code | call | result | log | done | error
//
// Yang diuji:
//   1. eksekusi string TS tanpa transpiler eksternal (data: URL import)
//   2. RPC dua arah lewat stdio, dengan id-matching (termasuk paralel)
//   3. console.log terpisah dari channel RPC
//   4. fetch() benar-benar melempar error

const enc = new TextEncoder();

function send(obj: unknown): void {
  Deno.stdout.writeSync(enc.encode(JSON.stringify(obj) + "\n"));
}

// --- S5: console.log dibajak jadi pesan RPC bertipe `log` ---------------
const rawLog = console.log;
console.log = (...args: unknown[]): void => {
  send({
    t: "log",
    data: args
      .map((a) => (typeof a === "string" ? a : JSON.stringify(a)))
      .join(" "),
  });
};

// --- reader loop + id matching ------------------------------------------
type Pending = { res: (v: unknown) => void; rej: (e: unknown) => void };
const pending = new Map<number, Pending>();
let seq = 0;

let resolveCode: (c: string) => void;
const codePromise = new Promise<string>((r) => (resolveCode = r));

(async () => {
  const dec = new TextDecoder();
  let buf = "";
  for await (const chunk of Deno.stdin.readable) {
    buf += dec.decode(chunk, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let m: Record<string, unknown>;
      try {
        m = JSON.parse(line);
      } catch {
        continue;
      }
      if (m.t === "code") {
        resolveCode(m.code as string);
      } else if (m.t === "result") {
        const p = pending.get(m.id as number);
        if (!p) continue;
        pending.delete(m.id as number);
        if (m.ok) {
          p.res(m.value);
        } else {
          const e = m.error as { type?: string; message?: string };
          const err = new Error(e?.message ?? "tool error");
          err.name = e?.type ?? "ToolError";
          Object.assign(err, e);
          p.rej(err);
        }
      }
    }
  }
})();

function callTool(server: string, tool: string, args: unknown): Promise<unknown> {
  const id = ++seq;
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    send({ t: "call", id, server, tool, args });
  });
}

// --- `tools` proxy: tools.<server>.<tool>(input) ------------------------
const tools: Record<string, Record<string, (a: unknown) => Promise<unknown>>> =
  new Proxy({}, {
    get: (_t, server: string) =>
      new Proxy({}, {
        get: (_t2, tool: string) => (args: unknown) =>
          callTool(server, tool, args),
      }),
  });

// @ts-ignore sengaja: kode model memakai `tools` sebagai global
globalThis.tools = tools;

// --- jalankan kode model ------------------------------------------------
const code = await codePromise;
const url = "data:application/typescript;base64," +
  btoa(String.fromCharCode(...enc.encode(code)));

try {
  await import(url);
  send({ t: "done" });
} catch (e) {
  send({
    t: "error",
    message: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
  });
}

rawLog; // tandai terpakai
Deno.exit(0);
