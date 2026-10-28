"""외부 구매(내자/외자) — 구매요청 → PO → 선적 → 입고 → 대금까지 PO 라인 단위로 관리.

purchase_lines(PO 라인, 구매 E2E 엑셀의 한 줄)와 purchase_receipts(입고 기록)가 원본이다.
입고수량·미입고·상태·리드타임은 저장하지 않고 조회할 때 입고 기록으로 계산한다.
재고이관(창고<>캠프, purchase_orders.py)과 달리 회사 바깥 거래처에서 사오는 구매다."""

import re
from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import text

from db import get_db_connection
from formatting import KST

# 국내 창고 입고분만 "현재고 + 입고예정"에 넣는다 (태국·LA·MEMPHIS… 해외 거점 입고는 제외).
DOMESTIC_PATTERN = "대구|한국"
STATUS_DONE, STATUS_PARTIAL, STATUS_ORDERED = "입고 완료", "부분 입고", "발주"
# 계약 납기가 없는 줄은 PO 승인일 + 3.5개월을 예상 입고일로 본다.
DEFAULT_LEAD = pd.DateOffset(months=3, days=15)


def _status(row):
    if row["qty"] > 0 and row["received_qty"] >= row["qty"]:
        return STATUS_DONE
    if row["received_qty"] > 0:
        return STATUS_PARTIAL
    s = row["excel_status"]
    # 입고 기록이 없는데 엑셀에 '입고 완료'라고 적힌 줄은 수량 기준으로 다시 판단한다.
    return s if isinstance(s, str) and s not in (STATUS_DONE, STATUS_PARTIAL) else STATUS_ORDERED


@st.cache_data(ttl=60, show_spinner=False)
def load_purchase_lines():
    """PO 라인 전체 + 입고 합계·미입고·상태·리드타임. SKU가 비면 부품 카탈로그(제조사PN)로 보완."""
    conn = get_db_connection()
    df = conn.query(
        """
        select l.*, coalesce(l.item_code, c.sku) as sku,
               coalesce(r.received_qty, 0) as received_qty, r.last_received_on, r.app_receipts
        from purchase_lines l
        left join parts_catalog c on c.package_no = l.mfr_pn
        left join (
            select line_id, sum(qty) as received_qty, max(received_on) as last_received_on,
                   count(*) filter (where source = 'app') as app_receipts
            from purchase_receipts group by line_id
        ) r on r.line_id = l.id
        order by l.po_approved_on desc nulls last, l.po_no, l.line_no
        """,
        ttl=0,
    )
    for col in ["qty", "received_qty", "unit_price", "amount", "fx_rate", "amount_krw"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["received_qty"] = df["received_qty"].fillna(0)
    df["open_qty"] = (df["qty"] - df["received_qty"]).clip(lower=0)
    df["status"] = df.apply(_status, axis=1) if not df.empty else pd.Series(dtype=str)
    for col in ["po_approved_on", "last_received_on", "contract_due", "pr_requested_on", "paid_on"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df["lead_days"] = (df["last_received_on"] - df["po_approved_on"]).dt.days.where(df["status"] == STATUS_DONE)
    today = pd.Timestamp(datetime.now(KST).date())
    df["expected_on"] = df["contract_due"].fillna(df["po_approved_on"] + DEFAULT_LEAD)
    df["overdue"] = (df["open_qty"] > 0) & df["expected_on"].notna() & (df["expected_on"] < today)
    df["po_year"] = df["po_approved_on"].dt.year.astype("Int64")
    df["domestic"] = df["destination"].fillna("").str.contains(DOMESTIC_PATTERN)
    return df


def incoming_for_sku(sku):
    """국내(대구·한국)로 들어올 미입고 줄 — 재고이관 화면에서 품목별 입고 예정을 보여줄 때 쓴다."""
    df = load_purchase_lines()
    key = str(sku or "").strip().upper()
    hit = (df["open_qty"] > 0) & df["domestic"] & (df["sku"].fillna("").str.strip().str.upper() == key)
    return df[hit].sort_values("expected_on")


def list_receipts(line_ids):
    if not line_ids:
        return pd.DataFrame()
    conn = get_db_connection()
    return conn.query(
        """
        select r.*, l.line_no, l.item_name from purchase_receipts r
        join purchase_lines l on l.id = r.line_id
        where r.line_id = any(:ids) order by r.received_on nulls first, r.id
        """,
        params={"ids": [int(i) for i in line_ids]},
        ttl=0,
    )


def add_receipt(line_id, qty, received_on, location, entered_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                "insert into purchase_receipts (line_id, qty, received_on, location, source, entered_by) "
                "values (:l, :q, :d, :loc, 'app', :by)"
            ),
            {"l": int(line_id), "q": qty, "d": received_on, "loc": location or None, "by": entered_by},
        )
        session.commit()
    load_purchase_lines.clear()


def delete_app_receipt(receipt_id):
    """앱에서 잘못 등록한 입고 취소 (엑셀에서 온 입고 기록은 지우지 않는다)."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text("delete from purchase_receipts where id = :id and source = 'app'"), {"id": int(receipt_id)}
        )
        session.commit()
    load_purchase_lines.clear()


def po_exists(po_no):
    conn = get_db_connection()
    return not conn.query(
        "select 1 from purchase_lines where trim(po_no) = :p limit 1", params={"p": po_no.strip()}, ttl=0
    ).empty


def _next_line_no(session):
    nums = session.execute(text("select line_no from purchase_lines where line_no ~ '^L[0-9]+$'")).scalars()
    return max([int(n[1:]) for n in nums] + [0]) + 1


def create_purchase_order(header, lines):
    """앱에서 만든 PO를 purchase_lines(source='app')에 줄마다 저장. 관리번호는 이어서 부여.
    header: {po_no, po_date, due_date, currency, supplier, supply_type, destination, buyer, note}"""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(text("lock table purchase_lines in share row exclusive mode"))
        n = _next_line_no(session)
        for i, ln in enumerate(lines):
            session.execute(
                text(
                    """
                    insert into purchase_lines
                        (line_no, po_no, po_approved_on, contract_due, currency, supplier, supply_type,
                         destination, buyer, note, item_code, item_name, mfr_pn, qty, unit_price, amount, source)
                    values (:line_no, :po_no, :po_date, :due_date, :currency, :supplier, :supply_type,
                            :destination, :buyer, :note, :item_code, :item_name, :mfr_pn, :qty, :unit_price,
                            :amount, 'app')
                    """
                ),
                {
                    **header, "line_no": f"L{n + i:04d}", "note": header.get("note") or None,
                    "item_code": ln.get("item_code"), "item_name": ln["item_name"],
                    "mfr_pn": ln.get("package_no"), "qty": ln["qty"], "unit_price": ln.get("unit_price"),
                    "amount": ln.get("amt"),
                },
            )
        session.commit()
    load_purchase_lines.clear()


def doc_po_number(po_no):
    """발주서에 찍는 번호: 원장 2026PO_034 -> GBIKE-2026PO_034 (FOC 등 다른 형식은 그대로)."""
    po_no = str(po_no or "").strip()
    return f"GBIKE-{po_no}" if re.fullmatch(r"\d{4}PO_\d+", po_no) else po_no


def next_po_number(year, template_po=None):
    """{year}PO_NNN 중 다음 번호 — 원장(엑셀에서 가져온 PO 포함)과 발주서 양식 H11 중 큰 값 다음."""
    conn = get_db_connection()
    df = conn.query(
        "select distinct trim(po_no) as po_no from purchase_lines where po_no like :p",
        params={"p": f"{year}PO_%"}, ttl=0,
    )
    seqs = [int(m.group(1)) for p in list(df["po_no"]) + [template_po]
            if (m := re.fullmatch(rf"(?:GBIKE-)?{year}PO_(\d+)", str(p or "").strip()))]
    return f"{year}PO_{max(seqs + [0]) + 1:03d}"
