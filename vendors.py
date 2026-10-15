"""거래처(공급업체) 마스터 — 구매발주 등록 시 선택하는 목록."""

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=30)
def list_vendors():
    conn = get_db_connection()
    return conn.query("select * from vendors order by name", ttl=0)


def create_vendor(name, vendor_type, contact, note):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into vendors (name, vendor_type, contact, note)
                values (:name, :vendor_type, :contact, :note)
                """
            ),
            {
                "name": name,
                "vendor_type": vendor_type,
                "contact": contact or None,
                "note": note or None,
            },
        )
        session.commit()


def update_vendor(vendor_id, vendor_type, contact, note):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update vendors set vendor_type = :vendor_type, contact = :contact, note = :note
                where id = :id
                """
            ),
            {"id": vendor_id, "vendor_type": vendor_type, "contact": contact or None, "note": note or None},
        )
        session.commit()


def delete_vendor(vendor_id):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(text("delete from vendors where id = :id"), {"id": vendor_id})
        session.commit()
