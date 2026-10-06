"""창고/캠프 재고 스냅샷 (일별 수량 추이, 주별 재고 금액 추이)."""

from datetime import datetime

import pandas as pd
from sqlalchemy import text

from db import get_db_connection


def save_warehouse_snapshot(total_qty, total_amt, item_count):
    """오늘 날짜로 창고 재고 스냅샷을 저장(upsert)한다. 박스히어로 API가 현재 시점만
    알려주기 때문에, 창고 현황 탭을 열 때마다 오늘자 스냅샷을 남겨 월별 추이를 쌓는다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into warehouse_snapshots (snapshot_date, total_qty, total_amt, item_count)
                values (current_date, :qty, :amt, :cnt)
                on conflict (snapshot_date) do update
                set total_qty = excluded.total_qty, total_amt = excluded.total_amt,
                    item_count = excluded.item_count, created_at = now()
                """
            ),
            {"qty": total_qty, "amt": total_amt, "cnt": item_count},
        )
        session.commit()


def load_warehouse_snapshots():
    conn = get_db_connection()
    return conn.query("select * from warehouse_snapshots order by snapshot_date", ttl=60)


def current_inventory_period():
    """(ISO 연도, ISO 주차, 'YYYY-Www' 표시용 라벨)을 반환."""
    iso_year, iso_week, _ = datetime.now().isocalendar()
    return iso_year, iso_week, f"{iso_year}-W{iso_week:02d}"


def save_inventory_value_snapshot(source, rows):
    """rows: [{"camp","item_code","item_name","qty","unit_price","amt"}, ...]로 이번 주(ISO 연도+주차)
    현황을 채운다 (이번 주 + 해당 source의 기존 행만 지우고 새로 append — 지난 주들의 누적 이력은
    그대로 남는다). 같은 주 안에서 여러 번 호출되면 그 주 행만 최신 값으로 덮어쓰고, 주가 바뀌면
    새 행 묶음이 추가된다 (예: 2026-W38 재고현황 800행, 2026-W39 재고현황 805행, ...).
    qty와 그 시점 단가(unit_price)를 따로 남겨서, 나중에 단가가 바뀌어도 qty에 원하는 기준
    단가를 곱해 같은 기준으로 재고 금액을 다시 계산할 수 있다.
    수천 건 단위라 upsert보다 delete+bulk append가 훨씬 빠르다."""
    if not rows:
        return
    conn = get_db_connection()
    year, week, label = current_inventory_period()
    df = pd.DataFrame(rows)
    df["year"] = year
    df["week"] = week
    df["period_label"] = label
    df["snapshot_date"] = datetime.now().date()
    df["source"] = source
    engine = conn.session.get_bind()
    with engine.begin() as connection:
        connection.execute(
            text("delete from inventory_value_snapshots where year = :y and week = :w and source = :s"),
            {"y": year, "w": week, "s": source},
        )
        df.to_sql(
            "inventory_value_snapshots", con=connection, if_exists="append", index=False,
            method="multi", chunksize=1000,
        )


def build_camp_value_snapshot_rows(data):
    rows = []
    for it in data["items"]:
        code = it.get("c")
        if not code:
            continue
        for camp, (q, a) in it.get("x", {}).items():
            if q == 0 and a == 0:
                continue
            rows.append(
                {
                    "camp": camp,
                    "item_code": code,
                    "item_name": it["n"],
                    "qty": q,
                    "unit_price": (a / q) if q else None,
                    "amt": a,
                }
            )
    return rows


def build_warehouse_value_snapshot_rows(wh_items):
    rows = []
    for it in wh_items:
        sku = it.get("sku")
        if not sku:
            continue
        qty = it.get("quantity", 0)
        price = float(it.get("price") or 0)
        rows.append(
            {
                "camp": "",
                "item_code": sku,
                "item_name": it.get("name"),
                "qty": qty,
                "unit_price": price if qty else None,
                "amt": qty * price,
            }
        )
    return rows


def load_inventory_value_trend():
    """주 x 구분(캠프/창고)별 재고 금액 합계 추이 (스냅샷 당시 단가 기준)."""
    conn = get_db_connection()
    return conn.query(
        """
        select year, week, period_label, max(snapshot_date) as snapshot_date, source,
               sum(qty) as qty, sum(amt) as amt
        from inventory_value_snapshots
        group by year, week, period_label, source
        order by year, week
        """,
        ttl=60,
    )
