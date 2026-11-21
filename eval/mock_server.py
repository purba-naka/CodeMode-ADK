"""Mock MCP server berparameter.

Kenapa ini ada: `@modelcontextprotocol/server-filesystem` punya ~11 tool dengan
schema satu-dua field, jadi **tidak bisa** menguji dua dari tiga klaim blog:

    "agents are able to handle many more tools, and more complex tools, when
     those tools are presented as a TypeScript API rather than directly"

Server ini punya dua knob:

    --tools 10|30|60          jumlah tool  -> klaim "many more tools"
    --schema flat|nested|oneof  kompleksitas -> klaim "more complex tools"

Tool yang berguna untuk task selalu ada dan namanya tetap, apa pun knob-nya.
Sisanya adalah pengecoh (distractor) dengan nama dan deskripsi yang masuk akal -
bukan `tool_1`, `tool_2`, karena nama yang jelas-jelas sampah membuat pemilihan
tool jadi terlalu mudah dan hasilnya tidak berarti.

Dijalankan sebagai stdio MCP server:
    python eval/mock_server.py --tools 30 --schema nested
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# ---------------------------------------------------------------- data uji
#: 12 pesanan. Jawaban tiap task diturunkan dari sini, jadi bisa diverifikasi.
ORDERS: list[dict[str, Any]] = [
    {
        "id": "A1",
        "region": "jakarta",
        "status": "pending",
        "total": 150_000,
        "items": 3,
    },
    {"id": "A2", "region": "jakarta", "status": "shipped", "total": 90_000, "items": 1},
    {
        "id": "A3",
        "region": "bandung",
        "status": "pending",
        "total": 310_000,
        "items": 7,
    },
    {
        "id": "A4",
        "region": "bandung",
        "status": "cancelled",
        "total": 40_000,
        "items": 2,
    },
    {
        "id": "A5",
        "region": "surabaya",
        "status": "pending",
        "total": 275_000,
        "items": 5,
    },
    {
        "id": "A6",
        "region": "surabaya",
        "status": "shipped",
        "total": 60_000,
        "items": 2,
    },
    {
        "id": "A7",
        "region": "jakarta",
        "status": "pending",
        "total": 420_000,
        "items": 9,
    },
    {"id": "A8", "region": "medan", "status": "shipped", "total": 120_000, "items": 4},
    {"id": "A9", "region": "medan", "status": "pending", "total": 85_000, "items": 2},
    {
        "id": "B1",
        "region": "jakarta",
        "status": "shipped",
        "total": 199_000,
        "items": 6,
    },
    {"id": "B2", "region": "bandung", "status": "pending", "total": 55_000, "items": 1},
    {
        "id": "B3",
        "region": "surabaya",
        "status": "cancelled",
        "total": 70_000,
        "items": 3,
    },
]

REGIONS = ["jakarta", "bandung", "surabaya", "medan"]

#: Antrian deploy untuk task control-flow: baru "complete" di polling ke-3.
_DEPLOY_POLLS = {"n": 0}

# -------------------------------------------------------- tool yang dipakai
#: Nama tool inti. Harus persis sama di semua konfigurasi grid supaya
#: perbandingan antar titik grid sahih.
CORE_TOOLS = [
    "list_regions",
    "get_orders_by_region",
    "get_order_detail",
    "get_region_summary",
    "poll_deploy_status",
]

#: Pengecoh: nama masuk akal, berisiko tertukar dengan tool inti.
DISTRACTORS = [
    ("search_invoices", "Cari invoice berdasarkan kata kunci."),
    ("get_invoice_detail", "Ambil detail satu invoice."),
    ("list_warehouses", "Daftar gudang yang aktif."),
    ("get_warehouse_stock", "Ambil stok satu gudang."),
    ("list_couriers", "Daftar kurir yang tersedia."),
    ("get_courier_rate", "Ambil tarif kurir untuk satu rute."),
    ("list_customers", "Daftar pelanggan terdaftar."),
    ("get_customer_profile", "Ambil profil satu pelanggan."),
    ("get_customer_orders", "Ambil riwayat pesanan satu pelanggan."),
    ("list_promotions", "Daftar promosi yang sedang berjalan."),
    ("get_promotion_detail", "Ambil detail satu promosi."),
    ("list_refunds", "Daftar permintaan refund."),
    ("get_refund_status", "Ambil status satu refund."),
    ("list_shipments", "Daftar pengiriman."),
    ("get_shipment_tracking", "Ambil pelacakan satu pengiriman."),
    ("list_suppliers", "Daftar pemasok."),
    ("get_supplier_contract", "Ambil kontrak satu pemasok."),
    ("list_returns", "Daftar barang yang dikembalikan."),
    ("get_return_reason", "Ambil alasan pengembalian."),
    ("list_tax_rules", "Daftar aturan pajak per wilayah."),
    ("get_tax_rate", "Ambil tarif pajak satu wilayah."),
    ("list_payment_methods", "Daftar metode pembayaran."),
    ("get_payment_status", "Ambil status satu pembayaran."),
    ("list_audit_events", "Daftar peristiwa audit."),
    ("get_audit_event", "Ambil satu peristiwa audit."),
    ("list_webhooks", "Daftar webhook terdaftar."),
    ("get_webhook_delivery", "Ambil riwayat pengiriman webhook."),
    ("list_api_keys", "Daftar kunci API."),
    ("get_rate_limit", "Ambil batas laju untuk satu kunci."),
    ("list_feature_flags", "Daftar feature flag."),
    ("get_flag_value", "Ambil nilai satu feature flag."),
    ("list_regions_legacy", "Daftar wilayah versi lama. Jangan dipakai lagi."),
    ("get_orders_legacy", "Ambil pesanan versi lama. Jangan dipakai lagi."),
    ("list_segments", "Daftar segmen pelanggan."),
    ("get_segment_members", "Ambil anggota satu segmen."),
    ("list_campaigns", "Daftar kampanye pemasaran."),
    ("get_campaign_metrics", "Ambil metrik satu kampanye."),
    ("list_channels", "Daftar kanal penjualan."),
    ("get_channel_report", "Ambil laporan satu kanal."),
    ("list_price_books", "Daftar price book."),
    ("get_price_entry", "Ambil satu entri harga."),
    ("list_bundles", "Daftar bundel produk."),
    ("get_bundle_items", "Ambil isi satu bundel."),
    ("list_categories", "Daftar kategori produk."),
    ("get_category_tree", "Ambil pohon kategori."),
    ("list_reviews", "Daftar ulasan produk."),
    ("get_review_detail", "Ambil detail satu ulasan."),
    ("list_tickets", "Daftar tiket dukungan."),
    ("get_ticket_thread", "Ambil percakapan satu tiket."),
    ("list_sla_targets", "Daftar target SLA."),
    ("get_sla_breach", "Ambil pelanggaran SLA."),
    ("list_forecasts", "Daftar ramalan penjualan."),
    ("get_forecast_detail", "Ambil detail satu ramalan."),
    ("list_budgets", "Daftar anggaran."),
    ("get_budget_line", "Ambil satu baris anggaran."),
]


# ------------------------------------------------------------------ schema
def _schema(mode: str, fields: dict[str, str], required: list[str]) -> dict[str, Any]:
    """Bangun inputSchema sesuai tingkat kompleksitas.

    - flat   : properti primitif di level atas
    - nested : properti dibungkus objek `filter`, plus opsi paging bersarang
    - oneof  : flat ditambah `selector` ber-oneOf dan enum besar
    """
    props: dict[str, Any] = {}
    for name, typ in fields.items():
        if name == "region":
            props[name] = {
                "type": "string",
                "description": "Nama wilayah.",
                "enum": REGIONS
                if mode != "oneof"
                else REGIONS + [f"zona_{i}" for i in range(20)],
            }
        else:
            props[name] = {"type": typ, "description": f"Parameter {name}."}

    if mode == "flat":
        return {"type": "object", "properties": props, "required": required}

    if mode == "nested":
        return {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "object",
                    "description": "Kriteria penyaringan.",
                    "properties": props,
                    "required": required,
                },
                "paging": {
                    "type": "object",
                    "description": "Opsi paginasi.",
                    "properties": {
                        "page": {"type": "integer", "description": "Mulai dari 1."},
                        "size": {"type": "integer", "description": "Maks 100."},
                    },
                },
            },
            "required": ["filter"],
        }

    # oneof
    return {
        "type": "object",
        "properties": {
            **props,
            "selector": {
                "description": "Pilih salah satu bentuk pemilihan.",
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"by_id": {"type": "string"}},
                        "required": ["by_id"],
                    },
                    {
                        "type": "object",
                        "properties": {
                            "by_range": {
                                "type": "object",
                                "properties": {
                                    "min": {"type": "number"},
                                    "max": {"type": "number"},
                                },
                                "required": ["min", "max"],
                            }
                        },
                        "required": ["by_range"],
                    },
                ],
            },
            "sort": {
                "type": "string",
                "enum": ["total_asc", "total_desc", "items_asc", "items_desc", "id"],
            },
        },
        "required": required,
    }


def _unwrap(mode: str, args: dict[str, Any]) -> dict[str, Any]:
    """Ambil argumen efektif, apa pun bentuk schema-nya."""
    if mode == "nested":
        return dict(args.get("filter") or {})
    return dict(args)


# ------------------------------------------------------------------- tools
def build_tools(n_tools: int, mode: str) -> list[dict[str, Any]]:
    """Daftar definisi tool untuk `tools/list`."""
    defs: list[dict[str, Any]] = [
        {
            "name": "list_regions",
            "description": (
                "Daftar semua wilayah yang punya pesanan. Panggil ini dulu "
                "kalau kamu belum tahu wilayah apa saja yang ada."
            ),
            "inputSchema": _schema(mode, {}, []),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "regions": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["regions"],
            },
        },
        {
            "name": "get_orders_by_region",
            "description": (
                "Ambil SEMUA pesanan di satu wilayah, termasuk yang sudah "
                "dikirim dan dibatalkan. Saring sendiri kalau hanya butuh "
                "status tertentu."
            ),
            "inputSchema": _schema(mode, {"region": "string"}, ["region"]),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "orders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "region": {"type": "string"},
                                "status": {"type": "string"},
                                "total": {"type": "number"},
                                "items": {"type": "integer"},
                            },
                            "required": ["id", "status", "total"],
                        },
                    }
                },
                "required": ["orders"],
            },
        },
        {
            "name": "get_order_detail",
            "description": "Ambil detail satu pesanan berdasarkan id.",
            "inputSchema": _schema(mode, {"order_id": "string"}, ["order_id"]),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "total": {"type": "number"},
                    "items": {"type": "integer"},
                    "region": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "get_region_summary",
            "description": (
                "Ringkasan satu wilayah: jumlah pesanan dan nilai totalnya, "
                "untuk SEMUA status."
            ),
            "inputSchema": _schema(mode, {"region": "string"}, ["region"]),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "count": {"type": "integer"},
                    "total": {"type": "number"},
                },
                "required": ["region", "count", "total"],
            },
        },
        {
            "name": "poll_deploy_status",
            "description": (
                "Cek status deploy saat ini. Mengembalikan 'running' sampai "
                "selesai, lalu 'complete'. Panggil ulang sampai 'complete'."
            ),
            "inputSchema": _schema(mode, {}, []),
            "outputSchema": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "attempt": {"type": "integer"},
                },
                "required": ["state"],
            },
        },
    ]

    need = max(0, n_tools - len(defs))
    for name, desc in DISTRACTORS[:need]:
        defs.append(
            {
                "name": name,
                "description": desc,
                "inputSchema": _schema(
                    mode, {"query": "string", "limit": "integer"}, []
                ),
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["items"],
                },
            }
        )
    return defs


def call_tool(name: str, args: dict[str, Any], mode: str) -> dict[str, Any]:
    a = _unwrap(mode, args)

    if name == "list_regions":
        return {"regions": REGIONS}

    if name == "get_orders_by_region":
        region = a.get("region")
        return {"orders": [o for o in ORDERS if o["region"] == region]}

    if name == "get_order_detail":
        oid = a.get("order_id")
        for o in ORDERS:
            if o["id"] == oid:
                return dict(o)
        raise ValueError(f"pesanan {oid!r} tidak ada")

    if name == "get_region_summary":
        region = a.get("region")
        rows = [o for o in ORDERS if o["region"] == region]
        return {
            "region": region,
            "count": len(rows),
            "total": sum(o["total"] for o in rows),
        }

    if name == "poll_deploy_status":
        _DEPLOY_POLLS["n"] += 1
        n = _DEPLOY_POLLS["n"]
        return {"state": "complete" if n >= 3 else "running", "attempt": n}

    if any(name == d[0] for d in DISTRACTORS):
        return {"items": []}

    raise ValueError(f"tool {name!r} tidak dikenal")


# -------------------------------------------------------------- stdio loop
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", type=int, default=10)
    ap.add_argument("--schema", choices=["flat", "nested", "oneof"], default="flat")
    ns = ap.parse_args()

    tools = build_tools(ns.tools, ns.schema)

    def send(obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, rid = req.get("method"), req.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-orders", "version": "0.1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        elif method == "tools/call":
            p = req.get("params") or {}
            try:
                out = call_tool(p.get("name"), p.get("arguments") or {}, ns.schema)
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(out)}],
                            "structuredContent": out,
                            "isError": False,
                        },
                    }
                )
            except Exception as e:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": str(e)}],
                            "isError": True,
                        },
                    }
                )
        elif rid is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"method {method} tidak ada"},
                }
            )


if __name__ == "__main__":
    main()
