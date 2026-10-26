"""재고이관(캠프<>캠프) 거절 분석 — 거절한 캠프가 그 품목을 거절 이후 실제로 쓰는지 추적.

단위는 (거절한 캠프, 품목) 쌍. 최초 거절일이 속한 주부터 그 캠프의 주간 사용량(usage_facts)을
따라가고, 비교용으로 거절 전 8주 주평균과 현재 보유 재고를 같이 본다 — 재고는 있는데 안 쓰면서
거절했다면 재고를 쥐고 있는 것.

사용량 주차(해당주)는 일요일 시작 주차(엑셀 WEEKNUM 기본)라서 날짜에서 바로 계산한다
(예: 2025-06-01(일) = 2025년 23주)."""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from db import get_db_connection
from formatting import KST

BEFORE_WEEKS = 8


def _jan1_offset(year):
    return (date(year, 1, 1).weekday() + 1) % 7  # 1월 1일이 일요일이면 0


def weeknum(d):
    """일요일 시작 주차 (엑셀 WEEKNUM(d, 1)과 같음)."""
    return (d.timetuple().tm_yday - 1 + _jan1_offset(d.year)) // 7 + 1


def week_start(year, week):
    return date(year, 1, 1) - timedelta(days=_jan1_offset(year)) + timedelta(weeks=week - 1)


def week_start_of(d):
    return d - timedelta(days=(d.weekday() + 1) % 7)


@st.cache_data(ttl=60, show_spinner=False)
def load_rejections():
    conn = get_db_connection()
    return conn.query(
        """
        select id, item_code, item_name, from_camp, to_camp, qty, requested_qty,
               approved_at, approved_by, reject_reason
        from transfer_requests where status = 'rejected'
        """,
        ttl=0,
    )


@st.cache_data(ttl=600, show_spinner=False)
def load_weekly_usage(camps, codes):
    """(캠프, 품목코드 대문자, 주 시작일)별 사용 수량. camps/codes는 캐시 키가 되도록 tuple."""
    if not camps or not codes:
        return pd.DataFrame(columns=["camp", "code", "week_start", "qty"])
    conn = get_db_connection()
    df = conn.query(
        """
        select camp, upper(item_code) as code, year, week, sum(qty) as qty
        from usage_facts
        where camp = any(:camps) and upper(item_code) = any(:codes)
        group by camp, upper(item_code), year, week
        """,
        params={"camps": list(camps), "codes": list(codes)},
        ttl=0,
    )
    if df.empty:
        return pd.DataFrame(columns=["camp", "code", "week_start", "qty"])
    df["week_start"] = [week_start(int(y), int(w)) for y, w in zip(df["year"], df["week"])]
    df["qty"] = pd.to_numeric(df["qty"])
    return df[["camp", "code", "week_start", "qty"]]


@st.cache_data(ttl=600, show_spinner=False)
def latest_usage_week():
    """사용량 데이터가 있는 마지막 주의 시작일 (없으면 None)."""
    conn = get_db_connection()
    r = conn.query(
        "select year, week from usage_facts order by year desc, week desc limit 1", ttl=0
    )
    return None if r.empty else week_start(int(r.iloc[0]["year"]), int(r.iloc[0]["week"]))


def build_tracking(rej, stock_lookup):
    """(거절한 캠프, 품목) 쌍별 추적 표와 주간 사용량 원본을 만든다.
    stock_lookup(camp, code_upper) -> 현재 보유 수량"""
    rej = rej.copy()
    rej["code"] = rej["item_code"].str.strip().str.upper()
    rej["date"] = pd.to_datetime(rej["approved_at"], utc=True).dt.tz_convert(KST).dt.date
    rej["rqty"] = pd.to_numeric(rej["requested_qty"]).fillna(pd.to_numeric(rej["qty"]))
    pairs = (
        rej.sort_values("date")
        .groupby(["from_camp", "code"], as_index=False)
        .agg(
            item_name=("item_name", "last"),
            reject_count=("id", "count"),
            reject_qty=("rqty", "sum"),
            first_date=("date", "min"),
            last_date=("date", "max"),
            requesters=("to_camp", lambda s: ", ".join(dict.fromkeys(s))),
            last_reason=("reject_reason", lambda s: next((x for x in reversed(list(s)) if pd.notna(x)), None)),
        )
    )
    weekly = load_weekly_usage(tuple(sorted(pairs["from_camp"].unique())), tuple(sorted(pairs["code"].unique())))
    last_wk = latest_usage_week()

    rows = []
    for p in pairs.itertuples():
        w0 = week_start_of(p.first_date)
        series = weekly[(weekly["camp"] == p.from_camp) & (weekly["code"] == p.code)]
        before = series[(series["week_start"] < w0) & (series["week_start"] >= w0 - timedelta(weeks=BEFORE_WEEKS))]
        after = series[series["week_start"] >= w0]
        n_after = 0 if last_wk is None or last_wk < w0 else (last_wk - w0).days // 7 + 1
        rows.append({
            "거절한 캠프": p.from_camp,
            "SKU": p.code,
            "품목명": p.item_name,
            "거절 횟수": p.reject_count,
            "거절 수량": p.reject_qty,
            "최초 거절일": p.first_date,
            "요청한 캠프": p.requesters,
            "현재 보유": stock_lookup(p.from_camp, p.code),
            "거절 전 주평균": before["qty"].sum() / BEFORE_WEEKS,
            "거절 후 주평균": (after["qty"].sum() / n_after) if n_after else None,
            "거절 후 누적 사용": after["qty"].sum() if n_after else None,
            "거절 후 데이터 주수": n_after,
            "최근 거절 사유": p.last_reason,
        })
    return pd.DataFrame(rows), weekly, last_wk
