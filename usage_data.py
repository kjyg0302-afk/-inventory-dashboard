"""usage_facts 테이블(로우 단위 사용량 사실 테이블) 기반 집계/조회 함수들."""

import json
from datetime import datetime

import pandas as pd
import streamlit as st
from sqlalchemy import text

from app_data import backup_data_before_overwrite, save_data
from boxhero import apply_boxhero_names_to_usage, get_boxhero_name_map
from db import get_db_connection
from parsing import parse_usage_transactions_raw, summarize_usage_facts
from usage_calc import get_usage_for_code


def save_usage_facts(facts_df):
    """usage_facts 테이블에서 facts_df에 들어있는 (year, week) 조합만 지우고 다시 채운다
    (주차 단위 교체). 1주치만 올리든, 과거 데이터까지 포함해 통째로 다시 올리든 포함된
    주만 최신화되고 나머지 주는 그대로 남는다."""
    conn = get_db_connection()
    engine = conn.session.get_bind()
    weeks = facts_df[["year", "week"]].drop_duplicates()
    with engine.begin() as connection:
        for _, row in weeks.iterrows():
            connection.execute(
                text("delete from usage_facts where year = :year and week = :week"),
                {"year": int(row["year"]), "week": int(row["week"])},
            )
        facts_df.to_sql("usage_facts", con=connection, if_exists="append", index=False, method="multi", chunksize=1000)


REPLACE_MIN_RATIO = 0.8


def refresh_usage_summary(reason):
    """usage_facts 전체로 사용량 요약(app_data 'usage')을 다시 계산해 저장하고 반환한다."""
    conn = get_db_connection()
    facts = conn.query(
        "select camp, item_code, item_name, year, month, week, qty, amt from usage_facts", ttl=0
    )
    summary, _ = apply_boxhero_names_to_usage(summarize_usage_facts(facts))
    backup_data_before_overwrite("usage", reason)
    save_data("usage", summary)
    return summary


def _rebuild_facts_weeks(connection, weeks):
    """usage_transactions(건별 로우)에서 해당 주들의 usage_facts(주간 합계)를 다시 만든다."""
    name_map = get_boxhero_name_map()
    for year, week in sorted(weeks):
        connection.execute(
            text("delete from usage_facts where year = :y and week = :w"), {"y": int(year), "w": int(week)}
        )
        agg = pd.read_sql(
            text(
                "select year, month, week, camp, item_code, sum(qty) as qty, sum(amt) as amt "
                "from usage_transactions where year = :y and week = :w "
                "group by year, month, week, camp, item_code"
            ),
            connection,
            params={"y": int(year), "w": int(week)},
        )
        if agg.empty:
            continue
        agg["item_name"] = agg["item_code"].str.strip().str.upper().map(name_map)
        agg.to_sql("usage_facts", con=connection, if_exists="append", index=False, method="multi", chunksize=1000)


def ingest_raw_usage(file, file_name, ingested_by, file_size=None, dry_run=False):
    """정비 원본 엑셀 한 개를 반영한다.

    파일에 들어있는 정비완료 '날짜' 단위로 건별 로우를 갈아끼운다 — 주 단위로 지우면 주 중간에서
    끝나는 파일 때문에 다른 날짜 데이터가 사라지므로. 단, 이미 저장된 그 날짜 건수보다 새 파일
    건수가 크게 적으면(REPLACE_MIN_RATIO 미만) 몇 건만 섞여 들어온 날짜로 보고 건드리지 않는다.
    그다음 바뀐 주의 주간 합계를 다시 만들고, 사용량 요약은 DB 전체로 다시 계산한다."""
    tx = parse_usage_transactions_raw(file)
    no_date = int(tx["service_date"].isna().sum())
    tx = tx[tx["service_date"].notna()]
    if tx.empty:
        raise ValueError("정비완료 날짜가 있는 사용 데이터가 없어요.")

    conn = get_db_connection()
    new_counts = tx.groupby("service_date").size()
    existing = conn.query(
        "select service_date, count(*) as n from usage_transactions where service_date = any(:ds) group by service_date",
        params={"ds": list(new_counts.index)},
        ttl=0,
    )
    old_counts = dict(zip(existing["service_date"], existing["n"]))
    replace_dates, skipped = [], []
    for d, n in new_counts.items():
        old = int(old_counts.get(d, 0))
        if old == 0 or n >= REPLACE_MIN_RATIO * old:
            replace_dates.append(d)
        else:
            skipped.append({"date": str(d), "new": int(n), "existing": old})

    tx_r = tx[tx["service_date"].isin(replace_dates)]
    weeks = set(zip(tx_r["year"].astype(int), tx_r["week"].astype(int)))
    report = {
        "date_min": min(new_counts.index),
        "date_max": max(new_counts.index),
        "rows_saved": len(tx_r),
        "rows_replaced_existing": int(sum(old_counts.get(d, 0) for d in replace_dates)),
        "dates_replaced": len(replace_dates),
        "weeks": sorted(weeks),
        "skipped": skipped,
        "no_date_rows": no_date,
    }
    if dry_run:
        return report
    engine = conn.session.get_bind()
    with engine.begin() as c:
        if replace_dates:
            c.execute(text("delete from usage_transactions where service_date = any(:ds)"), {"ds": replace_dates})
            tx_r.to_sql("usage_transactions", con=c, if_exists="append", index=False, method="multi", chunksize=1000)
            _rebuild_facts_weeks(c, weeks)
        c.execute(
            text(
                """
                insert into usage_ingest_log
                    (file_name, file_size, date_min, date_max, rows_saved, dates_replaced, skipped, ingested_by)
                values (:f, :s, :dmin, :dmax, :rows, :nd, CAST(:skipped AS jsonb), :by)
                """
            ),
            {
                "f": file_name, "s": file_size, "dmin": min(new_counts.index), "dmax": max(new_counts.index),
                "rows": len(tx_r), "nd": len(replace_dates), "skipped": json.dumps(skipped), "by": ingested_by,
            },
        )

    report["summary"] = refresh_usage_summary(f"사용량 원본 반영: {file_name}")
    return report


def list_ingested_files():
    conn = get_db_connection()
    return conn.query("select * from usage_ingest_log order by ingested_at desc", ttl=0)


@st.cache_data(ttl=600, show_spinner=False)
def load_usage_month_item():
    """월 x 품목별 캠프 사용 수량/금액 (전체 캠프 합산, usage_facts 기준)."""
    conn = get_db_connection()
    return conn.query(
        "select year, month, item_code, sum(qty) as qty, sum(amt) as amt "
        "from usage_facts group by year, month, item_code",
        ttl=0,
    )


def camp_weekly_usage_rate(usage, code, camp):
    """해당 SKU의 특정 캠프 기준 주 평균 사용량 (사용량 데이터 없으면 None)."""
    item_usage = get_usage_for_code(usage, code)
    if not item_usage:
        return None
    u = item_usage.get(camp)
    return u.get("avg") if u else None


def top_camp_items_recent(camp, limit=30):
    """usage_facts 기준, 해당 캠프의 최근 3개월(이번 달 제외) 사용량 상위 품목과 월평균을 반환."""
    conn = get_db_connection()
    now = datetime.now()
    sql = """
        with recent_months as (
            select distinct year, month from usage_facts
            where (year, month) < (:cur_year, :cur_month)
            order by year desc, month desc
            limit 3
        ),
        scoped as (
            select f.item_code, f.item_name, f.year, f.month, sum(f.qty) as month_qty
            from usage_facts f
            join recent_months rm on f.year = rm.year and f.month = rm.month
            where f.camp = :camp
            group by f.item_code, f.item_name, f.year, f.month
        )
        select item_code, item_name, round(sum(month_qty) / count(*), 2) as avg_qty
        from scoped
        group by item_code, item_name
        order by sum(month_qty) desc
        limit :limit
    """
    return conn.query(
        sql, params={"cur_year": now.year, "cur_month": now.month, "camp": camp, "limit": limit}, ttl=60
    )


def load_category_monthly_usage(item_codes):
    """SKU 목록(카테고리)에 대한 월별 사용 수량/금액 합계 (usage_facts 기준)."""
    if not item_codes:
        return pd.DataFrame(columns=["year", "month", "qty", "amt"])
    conn = get_db_connection()
    return conn.query(
        """
        select year, month, sum(qty) as qty, sum(amt) as amt
        from usage_facts
        where item_code = any(:codes)
        group by year, month
        order by year, month
        """,
        params={"codes": item_codes},
        ttl=30,
    )


def load_recent_monthly_usage_by_sku(item_codes, months=3):
    """SKU별 최근 N개월(이번 달 제외) 평균 사용 수량 (전체 캠프 합산, usage_facts 기준)."""
    if not item_codes:
        return pd.DataFrame(columns=["item_code", "avg_qty"])
    conn = get_db_connection()
    now = datetime.now()
    sql = """
        with recent_months as (
            select distinct year, month from usage_facts
            where (year, month) < (:cur_year, :cur_month)
            order by year desc, month desc
            limit :months
        ),
        scoped as (
            select f.item_code, f.year, f.month, sum(f.qty) as month_qty
            from usage_facts f
            join recent_months rm on f.year = rm.year and f.month = rm.month
            where f.item_code = any(:codes)
            group by f.item_code, f.year, f.month
        )
        select item_code, round(sum(month_qty) / count(*), 1) as avg_qty
        from scoped
        group by item_code
    """
    return conn.query(
        sql,
        params={"cur_year": now.year, "cur_month": now.month, "months": months, "codes": item_codes},
        ttl=30,
    )
