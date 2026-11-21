// Runtime sandbox. Dijalankan dengan SEMUA permission ditolak.
//
// Satu-satunya jalan keluar adalah stdio ke proses induk. Tidak ada jaringan,
// tidak ada filesystem, tidak ada spawn proses - jadi tidak ada port atau
// kredensial yang bisa disalahgunakan.
//
// Kode model dieksekusi lewat `data:` URL import, yang ditranspile Deno
// sendiri. Tanpa esbuild/swc, tanpa file temporer, tanpa sourcemap.

type Json = unknown;

interface InitMsg {
  t: "init";
  flat: boolean;
  manifest: Record<string, string[]>;
  max_calls: number;
  code: string;
}

interface ResultMsg {
  t: "result";
  id: number;
  ok: boolean;
  value?: Json;
  error?: {
    type?: string;
    message?: string;
    server?: string;
    tool?: string;
    status?: number;
    body?: string;
  };
}

const enc = new TextEncoder();

function send(obj: unknown): void {
  Deno.stdout.writeSync(enc.encode(JSON.stringify(obj) + "\n"));
}

// --- console -> pesan RPC bertipe -------------------------------------------
// Wajib: kalau stdout mentah dipakai sebagai protokol, log yang kebetulan
// berbentuk JSON akan mengacaukan parser host.
function fmt(args: unknown[]): string {
  return args
    .map((a) => {
      if (typeof a === "string") return a;
      if (a instanceof Error) return `${a.name}: ${a.message}`;
      try {
        return JSON.stringify(a);
      } catch {
        return String(a);
      }
    })
    .join(" ");
}

for (const stream of ["log", "error", "warn", "info", "debug"] as const) {
  // deno-lint-ignore no-explicit-any
  (console as any)[stream] = (...args: unknown[]) =>
    send({ t: "log", stream, data: fmt(args) });
}

// --- error tool --------------------------------------------------------------
class ToolError extends Error {
  readonly server?: string;
  readonly tool?: string;
  readonly status?: number;
  readonly body?: string;

  constructor(payload: NonNullable<ResultMsg["error"]>) {
    super(payload.message ?? "tool gagal");
    this.name = payload.type ?? "ToolError";
    this.server = payload.server;
    this.tool = payload.tool;
    this.status = payload.status;
    this.body = payload.body;
  }
}

// --- reader loop + id matching -----------------------------------------------
type Pending = { res: (v: Json) => void; rej: (e: unknown) => void };
const pending = new Map<number, Pending>();
let seq = 0;

let resolveInit!: (m: InitMsg) => void;
const initPromise = new Promise<InitMsg>((r) => {
  resolveInit = r;
});

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
      let m: InitMsg | ResultMsg;
      try {
        m = JSON.parse(line);
      } catch {
        continue; // host tidak akan mengirim sampah; abaikan saja
      }
      if (m.t === "init") {
        resolveInit(m);
      } else if (m.t === "result") {
        const p = pending.get(m.id);
        if (!p) continue;
        pending.delete(m.id);
        if (m.ok) p.res(m.value);
        else p.rej(new ToolError(m.error ?? { message: "tool gagal" }));
      }
    }
  }
  // stdin tertutup sebelum semua call dijawab: jangan menggantung.
  for (const [, p] of pending) {
    p.rej(new Error("host menutup koneksi sebelum call selesai"));
  }
  pending.clear();
})();

// --- `tools` dibangun dari manifest, bukan Proxy tangkap-semua ---------------
// Dengan objek konkret, `tools.salah_nama` bernilai undefined dan langsung
// memicu TypeError yang jelas - bukan RPC yang menggantung.
function buildTools(init: InitMsg) {
  let used = 0;

  const makeFn = (server: string, tool: string) => (args: unknown = {}) => {
    if (used >= init.max_calls) {
      return Promise.reject(
        new Error(
          `batas ${init.max_calls} tool call per eksekusi terlampaui ` +
            `(saat memanggil ${server}.${tool})`,
        ),
      );
    }
    used++;
    const id = ++seq;
    return new Promise<Json>((res, rej) => {
      pending.set(id, { res, rej });
      send({ t: "call", id, server, tool, args: args ?? {} });
    });
  };

  if (init.flat) {
    const flat: Record<string, unknown> = Object.create(null);
    for (const [server, names] of Object.entries(init.manifest)) {
      for (const name of names) flat[name] = makeFn(server, name);
    }
    return Object.freeze(flat);
  }

  const nested: Record<string, unknown> = Object.create(null);
  for (const [server, names] of Object.entries(init.manifest)) {
    const group: Record<string, unknown> = Object.create(null);
    for (const name of names) group[name] = makeFn(server, name);
    nested[server] = Object.freeze(group);
  }
  return Object.freeze(nested);
}

// --- jalankan -----------------------------------------------------------------
const init = await initPromise;

// deno-lint-ignore no-explicit-any
(globalThis as any).tools = buildTools(init);

// `encodeURIComponent` dipakai, bukan base64: aman untuk non-ASCII dan tidak
// kena batas stack seperti String.fromCharCode(...bytes) pada kode panjang.
const url = "data:application/typescript," + encodeURIComponent(init.code);

try {
  await import(url);
  send({ t: "done" });
} catch (e) {
  const err = e instanceof Error ? e : new Error(String(e));
  send({
    t: "error",
    message: `${err.name}: ${err.message}`,
    stack: err.stack?.slice(0, 4000),
  });
}

Deno.exit(0);
