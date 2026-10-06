"""수요 예측(demand_forecasts) 저장/조회."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


def save_demand_forecasts(rows):
    """rows: camp/item_code/item_name/forecast_year/forecast_month/predicted_qty/historical_avg_qty/entered_by
    딕셔너리 리스트. 같은 (캠프,품목,연,월) 조합이면 덮어쓴다."""
    if not rows:
        return
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into demand_forecasts
                    (camp, item_code, item_name, forecast_year, forecast_month,
                     predicted_qty, historical_avg_qty, entered_by)
                values (:camp, :item_code, :item_name, :forecast_year, :forecast_month,
                        :predicted_qty, :historical_avg_qty, :entered_by)
                on conflict (camp, item_code, forecast_year, forecast_month) do update
                set predicted_qty = excluded.predicted_qty,
                    historical_avg_qty = excluded.historical_avg_qty,
                    entered_by = excluded.entered_by,
                    updated_at = now()
                """
            ),
            rows,
        )
        session.commit()


@st.cache_data(ttl=15)
def load_demand_forecasts(camp, year, month):
    conn = get_db_connection()
    return conn.query(
        "select * from demand_forecasts where camp = :camp and forecast_year = :year and forecast_month = :month",
        params={"camp": camp, "year": year, "month": month},
        ttl=0,
    )


@st.cache_data(ttl=15)
def list_demand_forecasts(camp=None):
    conn = get_db_connection()
    if camp:
        return conn.query(
            "select * from demand_forecasts where camp = :camp "
            "order by forecast_year desc, forecast_month desc, item_code",
            params={"camp": camp},
            ttl=0,
        )
    return conn.query(
        "select * from demand_forecasts order by forecast_year desc, forecast_month desc, camp, item_code",
        ttl=0,
    )
