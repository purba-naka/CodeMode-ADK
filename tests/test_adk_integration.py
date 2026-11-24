"""Gerbang Fase 3 - integrasi ADK.

Tanpa LLM: `ExecuteCodeTool.run_async` dipanggil langsung dengan ToolContext
sungguhan, persis seperti flow memanggilnya.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool

from codemode.adk import CodeMode, derive_tool_context
from codemode.catalog import DirectPolicy
from codemode.sandbox import Limits

TRACE: list[str] = []


# --------------------------------------------------------------- tool palsu
def baca(path: str) -> dict:
    """Baca berkas.

    Args:
        path: lokasi berkas.
    """
    TRACE.append(f"baca({path})")
    return {"content": f"isi {path}"}


def daftar(path: str) -> dict:
    """Daftar isi direktori.

    Args:
        path: lokasi direktori.
    """
    TRACE.append(f"daftar({path})")
    return {"entries": [f"{path}/a", f"{path}/b", f"{path}/c"]}


def tulis_state(key: str, value: str) -> dict:
    """Tulis ke session state.

    Args:
        key: kunci.
        value: nilai.
    """
    return {"ok": True, "_marker": (key, value)}


def rusak(x: int) -> dict:
    """Selalu gagal.

    Args:
        x: diabaikan.
    """
    raise RuntimeError("upstream mati")


async def _ctx(agent: LlmAgent) -> Context:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="f3", user_id="u1")
    ictx = InvocationContext(
        invocation_id="inv-1", agent=agent, session=session, session_service=svc
    )
    return Context(ictx, function_call_id="fc-execute-code")


def _agent(cm: CodeMode, **kw: Any) -> LlmAgent:
    return LlmAgent(name="a", model="gemini-2.0-flash", tools=[cm.tool], **kw)


async def _run(cm: CodeMode, agent: LlmAgent, code: str) -> dict:
    ctx = await _ctx(agent)
    return await cm.tool.run_async(args={"code": code}, tool_context=ctx)


@pytest.fixture
def cm() -> CodeMode:
    TRACE.clear()
    return CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(daftar), FunctionTool(rusak)]},
        limits=Limits(timeout_s=30, max_calls=10),
    )


# -------------------------------------------------------------- jalur dasar
async def test_end_to_end_rantai(cm):
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
const d = await tools.daftar({ path: "/data" });
const f = await tools.baca({ path: d.entries[0] });
console.log("n:", d.entries.length, "| isi:", f.content);
""",
    )
    assert out["status"] == "done", out
    assert out["tool_calls"] == 2
    assert "n: 3 | isi: isi /data/a" in out["output"]
    assert TRACE == ["daftar(/data)", "baca(/data/a)"]


async def test_instruction_memuat_deklarasi(cm):
    block = await cm.instruction_block()
    assert "declare const tools" in block
    assert "baca: (input: BacaInput)" in block
    assert "console.log" in block


async def test_instruction_provider_async(cm):
    provider = cm.instruction_provider("Kamu asisten.")
    agent = LlmAgent(
        name="a", model="gemini-2.0-flash", tools=[cm.tool], instruction=provider
    )
    from google.adk.agents.readonly_context import ReadonlyContext

    text, bypass = await agent.canonical_instruction(ReadonlyContext(None))
    assert text.startswith("Kamu asisten.")
    assert "declare const tools" in text
    assert bypass is True


# ------------------------------------------------- rantai callback (S2b)
async def test_callback_jalan_untuk_tool_di_dalam_sandbox(cm):
    fired: list[str] = []

    def before(*, tool, args, tool_context):
        fired.append(f"before:{tool.name}")
        return None

    def after(*, tool, args, tool_context, tool_response):
        fired.append(f"after:{tool.name}")
        return None

    agent = _agent(cm, before_tool_callback=before, after_tool_callback=after)
    out = await _run(cm, agent, 'console.log((await tools.baca({path:"/x"})).content);')

    assert out["status"] == "done", out
    assert fired == ["before:baca", "after:baca"], (
        "callback tidak jalan untuk tool di dalam sandbox - audit per-tool mati"
    )


async def test_before_callback_bisa_membatalkan_tool(cm):
    """Dasar deny-rule / HITL."""

    def deny(*, tool, args, tool_context):
        if args.get("path", "").startswith("/rahasia"):
            return {"error": "ditolak kebijakan"}
        return None

    agent = _agent(cm, before_tool_callback=deny)
    out = await _run(
        cm,
        agent,
        """
const a = await tools.baca({ path: "/rahasia/x" });
console.log("hasil:", JSON.stringify(a));
""",
    )
    assert out["status"] == "done", out
    assert "ditolak kebijakan" in out["output"]
    assert TRACE == [], "tool tetap dijalankan padahal sudah ditolak"


async def test_after_callback_bisa_meredaksi(cm):
    def redact(*, tool, args, tool_context, tool_response):
        return {"content": "[DIREDAKSI]"}

    agent = _agent(cm, after_tool_callback=redact)
    code = 'console.log((await tools.baca({path:"/pii"})).content);'
    out = await _run(cm, agent, code)
    assert "[DIREDAKSI]" in out["output"]


async def test_kegagalan_tool_di_raise_ke_sandbox(cm):
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
try {
  await tools.rusak({ x: 1 });
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("raised:", e.name, "-", e.message);
}
""",
    )
    assert "BUG" not in out["output"]
    assert "raised: RuntimeError - upstream mati" in out["output"]


async def test_on_tool_error_mengubah_jadi_nilai(cm):
    def on_err(*, tool, args, tool_context, error):
        return {"ditangani": str(error)}

    agent = _agent(cm, on_tool_error_callback=on_err)
    out = await _run(
        cm,
        agent,
        """
const r = await tools.rusak({ x: 1 });
console.log("tidak raise:", JSON.stringify(r));
""",
    )
    assert out["status"] == "done", out
    assert "upstream mati" in out["output"]


# ------------------------------------------------------------- atribusi
async def test_function_call_id_berbeda_per_call():
    """Punya parent milik `execute_code`; tool dalam sandbox harus punya
    id sendiri, atau alur auth/konfirmasi salah atribusi."""
    agent = LlmAgent(name="a", model="gemini-2.0-flash")
    parent = await _ctx(agent)
    a = derive_tool_context(parent, call_id="1")
    b = derive_tool_context(parent, call_id="2")

    assert parent.function_call_id == "fc-execute-code"
    assert a.function_call_id == "fc-execute-code#1"
    assert b.function_call_id == "fc-execute-code#2"
    assert a.function_call_id != b.function_call_id


async def test_event_actions_dibagi_agar_state_tidak_hilang():
    """Jebakan senyap: kalau turunan membuat EventActions baru, state delta
    dan artifact delta dari tool dalam sandbox akan hilang, karena flow hanya
    mempersistensi EventActions milik `execute_code`."""
    agent = LlmAgent(name="a", model="gemini-2.0-flash")
    parent = await _ctx(agent)
    child = derive_tool_context(parent, call_id="1")

    assert child.actions is parent.actions

    child.state["dari_sandbox"] = "nilai"
    assert parent.actions.state_delta["dari_sandbox"] == "nilai"
    assert parent.state["dari_sandbox"] == "nilai"


# --------------------------------------------- Fase 5: jalur direct
async def test_tool_butuh_konfirmasi_dirutekan_ke_direct():
    """Sebelum Fase 5 tool ini dibuang. Sekarang dialihkan ke jalur direct:
    tetap tidak terlihat dari sandbox, tapi tetap bisa dipakai model - dan
    `require_confirmation` ditangani oleh flow ADK, bukan oleh kami."""
    tool = FunctionTool(baca, require_confirmation=True)
    cm = CodeMode({"fs": [tool]})
    catalog = await cm.prepare()

    assert catalog.tools == []
    assert len(catalog.direct) == 1
    assert "require_confirmation" in catalog.direct[0].reason
    assert "baca" not in cm.declarations
    assert [t.name for t in await cm.direct_tools()] == ["baca"]


async def test_direct_eksplisit_dihapus_dari_sandbox_tapi_dideklarasikan():
    cm = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(tulis_state)]},
        direct=["tulis_state"],
        limits=Limits(timeout_s=30),
    )
    catalog = await cm.prepare()

    assert [s.name for s in catalog.tools] == ["baca"]
    assert [d.name for d in catalog.direct] == ["tulis_state"]

    # tidak di deklarasi TypeScript
    assert "tulis_state" not in cm.declarations
    # tapi ada di instruction, sebagai tool yang dipanggil langsung
    block = await cm.instruction_block()
    assert "Tool yang dipanggil langsung" in block
    assert "`tulis_state`" in block

    # dan tetap dideklarasikan ke model lewat toolset
    names = [t.name for t in await cm.direct_toolset.get_tools()]
    assert names == ["tulis_state"]
    assert [getattr(t, "name", None) for t in cm.tools[:1]] == ["execute_code"]


async def test_direct_juga_terima_nama_berkualifikasi_dan_predikat():
    cm = CodeMode({"fs": [FunctionTool(baca)]}, direct=["fs.baca"])
    assert [d.name for d in (await cm.prepare()).direct] == ["baca"]

    cm2 = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(daftar)]},
        direct=lambda tool, server: "uji" if tool.name.startswith("d") else None,
    )
    cat2 = await cm2.prepare()
    assert [d.name for d in cat2.direct] == ["daftar"]
    assert [s.name for s in cat2.tools] == ["baca"]


async def test_direct_tidak_bisa_dipanggil_dari_sandbox():
    """Jalur normal: namanya tidak ada di manifest, jadi TypeError di sandbox."""
    cm = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(tulis_state)]},
        direct=["tulis_state"],
        limits=Limits(timeout_s=30),
    )
    agent = _agent(cm)
    out = await _run(cm, agent, 'await tools.tulis_state({ key: "k", value: "v" });')
    assert out["status"] == "error"
    assert "TypeError" in out.get("error", "")
    assert out["tool_calls"] == 0


async def test_direct_ditolak_host_walau_protokol_dipalsukan():
    """Batas sesungguhnya ada di host. `console.log` dibajak runtime, tapi
    `Deno.stdout.writeSync` tidak - jadi kode model bisa menyuntik pesan
    `call` untuk tool direct. Harus ditolak di `RpcBridge`, bukan dipercayakan
    ke manifest."""
    cm = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(tulis_state)]},
        direct=["tulis_state"],
        limits=Limits(timeout_s=30),
    )
    terlihat: list[str] = []

    def audit(*, tool, args, tool_context):
        terlihat.append(tool.name)
        return None

    agent = _agent(cm, before_tool_callback=audit)
    out = await _run(
        cm,
        agent,
        """
const enc = new TextEncoder();
Deno.stdout.writeSync(enc.encode(JSON.stringify({
  t: "call", id: 1, server: "fs", tool: "tulis_state",
  args: { key: "k", value: "v" },
}) + "\\n"));
await new Promise((r) => setTimeout(r, 800));
console.log("sesudah suntikan");
""",
    )
    # Host MENGHITUNG call palsu itu (jadi batas max_calls tetap berlaku)
    # tapi tool direct tidak pernah masuk rantai callback, apalagi dijalankan.
    assert out["tool_calls"] == 1, out
    assert terlihat == [], terlihat


async def test_bridge_menolak_tool_direct_secara_langsung():
    """Probe negatif tingkat bridge: tanpa sandbox, langsung ke `_run`."""
    from codemode.adk.bridge import DirectOnlyError, RpcBridge

    cm = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(tulis_state)]},
        direct=["tulis_state"],
    )
    catalog = await cm.prepare()
    bridge = RpcBridge(catalog, cm._tool_map)
    agent = _agent(cm)
    ctx = await _ctx(agent)

    # tool yang di sandbox tetap jalan
    assert await bridge.invoker(ctx)("fs", "baca", {"path": "/p"})

    with pytest.raises(DirectOnlyError):
        await bridge.invoker(ctx)("fs", "tulis_state", {"key": "k", "value": "v"})


async def test_validasi_argumen_code(cm):
    agent = _agent(cm)
    for bad in [{}, {"code": ""}, {"code": "   "}, {"code": 123}]:
        ctx = await _ctx(agent)
        out = await cm.tool.run_async(args=bad, tool_context=ctx)
        assert out["status"] == "error"


async def test_nama_tool_salah_tidak_sampai_ke_bridge(cm):
    agent = _agent(cm)
    out = await _run(cm, agent, "await tools.tidak_ada({});")
    assert out["status"] == "error"
    assert "TypeError" in out.get("error", "")
    assert out["tool_calls"] == 0


# ---------------------------------------------------------------- laporan
async def test_report(cm):
    agent = _agent(cm)
    await _run(cm, agent, 'console.log("x");')
    rep = cm.report()
    assert rep["tools"] == 3
    assert rep["servers"] == ["fs"]
    assert rep["executions"] == 1
    assert rep["declaration_chars"] > 100


# ------------------------------------------- temuan Fase 3: isError MCP
def gagal_ala_mcp(path: str) -> dict:
    """Tiru envelope CallToolResult yang gagal.

    Args:
        path: lokasi berkas.
    """
    TRACE.append(f"gagal_ala_mcp({path})")
    return {
        "content": [{"type": "text", "text": f"ENOENT: no such file {path}"}],
        "isError": True,
    }


async def test_is_error_mcp_diubah_jadi_exception():
    """Terukur di server MCP sungguhan: `McpTool.run_async` TIDAK melempar
    exception saat server melaporkan kegagalan - hasilnya kembali sebagai
    nilai normal dengan `isError: true`.

    Di direct-mode itu tidak berbahaya. Di code mode fatal: kode sandbox
    membaca return value dan lanjut seolah berhasil.
    """
    cm = CodeMode({"fs": [FunctionTool(gagal_ala_mcp)]}, limits=Limits(timeout_s=30))
    agent = _agent(cm)
    out = await _run(
        cm,
        agent,
        """
try {
  await tools.gagal_ala_mcp({ path: "/hilang" });
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("raised:", e.name, "|", e.message);
}
""",
    )
    assert out["status"] == "done", out
    assert "BUG" not in out["output"]
    assert "McpToolFailed" in out["output"]
    assert "ENOENT" in out["output"]


async def test_after_callback_bisa_membersihkan_is_error():
    """Raise terjadi SETELAH rantai callback, jadi callback redaksi masih
    bisa menganggap call ini berhasil - sama seperti direct-mode ADK, yang
    memperlakukan hasil ber-isError sebagai hasil sukses."""

    def sembuhkan(*, tool, args, tool_context, tool_response):
        return {"content": [{"type": "text", "text": "diperbaiki"}]}

    cm = CodeMode({"fs": [FunctionTool(gagal_ala_mcp)]}, limits=Limits(timeout_s=30))
    agent = _agent(cm, after_tool_callback=sembuhkan)
    out = await _run(
        cm,
        agent,
        """
const r = await tools.gagal_ala_mcp({ path: "/hilang" });
console.log("tidak raise:", r.content[0].text);
""",
    )
    assert out["status"] == "done", out
    assert "tidak raise: diperbaiki" in out["output"]


# ------------------------------------ Fase 5: kebijakan anotasi MCP
class _FakeAnn:
    def __init__(self, read_only=None, destructive=None):
        self.readOnlyHint = read_only
        self.destructiveHint = destructive


class _FakeMcp:
    def __init__(self, ann):
        self.annotations = ann
        self.inputSchema = {"type": "object", "properties": {}}
        self.outputSchema = None


def _beranotasi(fn, ann):
    tool = FunctionTool(fn)
    tool._mcp_tool = _FakeMcp(ann)
    return tool


async def test_policy_off_mengabaikan_anotasi():
    """Default tidak boleh mengubah perilaku Fase 0-4."""
    tool = _beranotasi(baca, _FakeAnn(read_only=False, destructive=True))
    cat = await CodeMode({"fs": [tool]}).prepare()
    assert [s.name for s in cat.tools] == ["baca"]
    assert cat.direct == []


async def test_policy_annotations_hanya_menarik_yang_bertanda():
    tools = [
        _beranotasi(baca, _FakeAnn(read_only=True)),
        _beranotasi(daftar, _FakeAnn(read_only=False, destructive=True)),
        FunctionTool(tulis_state),  # tanpa anotasi sama sekali
    ]
    cm = CodeMode({"fs": tools}, direct_policy=DirectPolicy.ANNOTATIONS)
    cat = await cm.prepare()
    assert [d.name for d in cat.direct] == ["daftar"]
    assert [s.name for s in cat.tools] == ["baca", "tulis_state"]


async def test_policy_strict_menganggap_anotasi_absen_destruktif():
    """Spesifikasi MCP: `destructiveHint` default-nya true. Mode strict setia
    pada itu, dan konsekuensinya memang keras."""
    tools = [
        _beranotasi(baca, _FakeAnn(read_only=True)),
        FunctionTool(tulis_state),
        _beranotasi(daftar, _FakeAnn(read_only=False)),
    ]
    cat = await CodeMode(
        {"fs": tools}, direct_policy=DirectPolicy.ANNOTATIONS_STRICT
    ).prepare()
    assert [s.name for s in cat.tools] == ["baca"]
    assert sorted(d.name for d in cat.direct) == ["daftar", "tulis_state"]


async def test_anotasi_hanya_memperketat_tidak_melonggarkan():
    """Spec: anotasi adalah hint dari server yang mungkin tidak dipercaya.
    Server yang mengaku `readOnlyHint=true` tidak boleh bisa menarik dirinya
    keluar dari daftar direct yang diminta pemilik agent."""
    tool = _beranotasi(tulis_state, _FakeAnn(read_only=True))
    cat = await CodeMode(
        {"fs": [tool]},
        direct=["tulis_state"],
        direct_policy=DirectPolicy.ANNOTATIONS,
    ).prepare()
    assert cat.tools == []
    assert [d.name for d in cat.direct] == ["tulis_state"]


# -------------------------------------------- Fase 5c: precheck type-check
DEKLARASI = """
interface BacaInput { path: string; head?: number }
interface BacaOutput { teks: string }
declare const tools: {
  baca: (input: BacaInput) => Promise<BacaOutput>;
};
"""


@pytest.fixture(scope="module")
def checker():
    from codemode.sandbox import TypeChecker

    tc = TypeChecker(DEKLARASI)
    yield tc
    tc.close()


async def test_precheck_meloloskan_kode_valid(checker):
    r = await checker.check(
        'const x = await tools.baca({ path: "/a", head: 2 });\nconsole.log(x.teks);'
    )
    assert r.ok, r.errors
    assert r.checked


@pytest.mark.parametrize(
    ("nama", "kode", "ts"),
    [
        ("tool tidak ada", 'await tools.bacaa({ path: "/a" });', "TS2"),
        ("argumen wajib hilang", "await tools.baca({});", "TS2345"),
        ("tipe salah", "await tools.baca({ path: 1 });", "TS2322"),
        (
            "properti hasil tidak ada",
            'const r = await tools.baca({ path: "/a" });\nconsole.log(r.tidakAda);',
            "TS2339",
        ),
        (
            "lupa await",
            'const r = tools.baca({ path: "/a" });\nconsole.log(r.teks);',
            "TS2339",
        ),
    ],
)
async def test_precheck_menolak_kode_rusak(checker, nama, kode, ts):
    r = await checker.check(kode)
    assert not r.ok, f"{nama} LOLOS padahal rusak"
    assert ts in r.errors, r.errors
    # Path direktori temp tidak boleh ikut terkirim ke model.
    assert "codemode_check_" not in r.errors


async def test_precheck_mencegah_eksekusi_dan_tool_call():
    """Nilai utamanya bukan pesan error, tapi NOL efek samping: tanpa ini,
    rantai yang separuhnya sah akan menjalankan call pertamanya lalu gagal di
    tengah - dan tool yang sudah jalan tidak bisa dibatalkan."""
    terpanggil: list[str] = []

    def audit(*, tool, args, tool_context):
        terpanggil.append(tool.name)
        return None

    cm = CodeMode(
        {"fs": [FunctionTool(baca), FunctionTool(daftar)]},
        limits=Limits(timeout_s=30),
        precheck=True,
    )
    await cm.prepare()
    agent = _agent(cm, before_tool_callback=audit)
    ctx = await _ctx(agent)

    kode = """
const a = await tools.daftar({ path: "/x" });
console.log(a);
await tools.baca({ path: 999 });
"""
    out = await cm.tool.run_async(args={"code": kode}, tool_context=ctx)

    assert out["status"] == "type_error", out
    assert out["tool_calls"] == 0
    assert terpanggil == [], terpanggil
    assert "TIDAK dijalankan" in out["error"]
    assert cm.report()["precheck"] == {
        "aktif": True,
        "dijalankan": 1,
        "ditolak": 1,
        "dilewati": 0,
    }
    await cm.close()


async def test_precheck_default_mati():
    """Default sengaja mati: menyalakannya mengubah kondisi pengukuran Fase 4."""
    cm = CodeMode({"fs": [FunctionTool(baca)]}, limits=Limits(timeout_s=30))
    await cm.prepare()
    assert cm.report()["precheck"] == {"aktif": False}

    # dan kode yang tidak lolos type-check tetap dijalankan (gagal di runtime)
    agent = _agent(cm)
    out = await _run(cm, agent, "await tools.tidak_ada({});")
    assert out["status"] == "error"
    assert "TypeError" in out.get("error", "")


# ------------------- regresi: nama field MCP berubah antar versi SDK
class _RawSnake:
    """Bentuk `mcp.types.Tool` pada mcp 2.x - snake_case."""

    def __init__(self):
        self.input_schema = {
            "type": "object",
            "properties": {"full_name": {"type": "string"}},
            "required": ["full_name"],
        }
        self.output_schema = {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        }
        self.annotations = None


class _RawCamel:
    """Bentuk pada mcp 1.26 - camelCase."""

    def __init__(self):
        self.inputSchema = {
            "type": "object",
            "properties": {"full_name": {"type": "string"}},
        }
        self.outputSchema = {
            "type": "object",
            "properties": {"result": {"type": "string"}},
        }
        self.annotations = None


@pytest.mark.parametrize("raw_cls", [_RawSnake, _RawCamel])
async def test_output_schema_terbaca_di_kedua_versi_mcp(raw_cls):
    """mcp 2.3 mengubah `outputSchema` -> `output_schema`. Membaca satu nama
    saja membuat tipe mundur ke index signature TANPA error - kegagalan senyap
    yang memaksa model menebak bentuk hasil.

    Terukur di server sungguhan: 20/20 tool punya output_schema, 0/20 terbaca
    lewat nama camelCase."""
    from codemode.catalog import SchemaSource, spec_from_tool

    tool = FunctionTool(baca)
    tool._mcp_tool = raw_cls()
    spec = spec_from_tool(tool, "srv")

    assert spec.output_source == SchemaSource.MCP_PRIVATE, (
        f"{raw_cls.__name__}: output_schema tidak terbaca"
    )
    teks = json.dumps(spec.output_schema)
    assert '"result"' in teks


async def test_tipe_output_konkret_muncul_di_deklarasi():
    """Probe negatif tingkat codegen: kalau output_schema hilang, deklarasi
    jatuh ke index signature dan model kehilangan tipe hasil."""
    tool = FunctionTool(baca)
    tool._mcp_tool = _RawSnake()
    cm = CodeMode({"srv": [tool]})
    await cm.prepare()

    assert "result" in cm.declarations
    assert "structuredContent" in cm.declarations


# ------------------------------------- F6-2: satu titik klasifikasi kegagalan
def test_classify_semua_dialek():
    from codemode.adk.outcome import classify

    assert (
        classify({"content": [{"text": "x"}], "isError": True}).kind == "mcp_is_error"
    )
    assert classify({"error": "arg salah"}).kind == "adk_error_dict"
    assert classify({"error": "x", "status": "e"}).kind == "adk_error_dict"
    assert classify({"error": "x", "rows": [1]}) is None  # data sah
    assert classify({"error": "x"}, from_tool=False) is None  # balasan callback
    assert classify({"isError": True}, from_tool=False).kind == "mcp_is_error"
    assert classify("teks") is None and classify(None) is None


def argumen_buruk(path: str) -> dict:
    """Tiru FunctionTool ADK yang menolak argumen dengan {"error": ...}.

    Args:
        path: lokasi berkas.
    """
    return {"error": "argumen tidak valid"}


async def test_error_dict_adk_diubah_jadi_exception():
    """Celah yang ditemukan dengan membaca kode PINTER, bukan oleh test kita:
    ADK mengembalikan {"error": ...} alih-alih melempar, dan sebelum F6-2 itu
    lolos ke sandbox sebagai nilai sukses."""
    cm = CodeMode({"fs": [FunctionTool(argumen_buruk)]}, limits=Limits(timeout_s=30))
    out = await _run(
        cm,
        _agent(cm),
        """
try {
  await tools.argumen_buruk({ path: "/x" });
  console.log("BUG: lanjut seolah berhasil");
} catch (e) {
  console.log("raised:", e.name, "|", e.message);
}
""",
    )
    assert "BUG" not in out["output"], out
    assert "adk_error_dict" in out["output"]
