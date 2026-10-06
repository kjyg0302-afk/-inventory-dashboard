"""usage_facts 테이블(로우 단위 사용량 사실 테이블) 기반 집계/조회 함수들."""

from datetime import datetime

import pandas as pd
from sqlalchemy import text

from db import get_db_connection
from usage_calc import get_usage_for_code


def save_usage_facts(facts_df):
    """usage_facts 테이블을 사용량 엑셀 기준으로 통째로 새로 채운다 (전체 교체)."""
    conn = get_db_connection()
    engine = conn.session.get_bind()
    with engine.begin() as connection:
        connection.execute(text("truncate table usage_facts"))
        facts_df.to_sql("usage_facts", con=connection, if_exists="append", index=False, method="multi", chunksize=1000)


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
