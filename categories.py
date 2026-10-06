"""SKU 카테고리 — 임의로 SKU 묶음을 만들어 재고/사용 금액을 따로 본다."""

import pandas as pd
import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=30)
def list_categories():
    conn = get_db_connection()
    df = conn.query("select distinct category from sku_categories order by category", ttl=0)
    return df["category"].tolist()


@st.cache_data(ttl=30)
def list_category_items(category):
    conn = get_db_connection()
    df = conn.query(
        "select item_code from sku_categories where category = :c order by item_code",
        params={"c": category},
        ttl=0,
    )
    return df["item_code"].tolist()


def save_category_items(category, item_codes):
    """category에 속하는 SKU 목록을 통째로 새로 채운다 (기존 목록은 지우고 새로 넣음)."""
    conn = get_db_connection()
    engine = conn.session.get_bind()
    df = pd.DataFrame({"category": category, "item_code": sorted(set(item_codes))})
    with engine.begin() as connection:
        connection.execute(text("delete from sku_categories where category = :c"), {"c": category})
        if not df.empty:
            df.to_sql("sku_categories", con=connection, if_exists="append", index=False, method="multi")


def delete_category(category):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(text("delete from sku_categories where category = :c"), {"c": category})
        session.commit()
