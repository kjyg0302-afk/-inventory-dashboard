"""app_data 테이블(키/JSON 블롭 저장소) 공용 헬퍼 + 그 위에 얹은 백업/탭권한 기능."""

import json

import streamlit as st
from sqlalchemy import text

from db import get_db_connection


def load_data(key):
    """app_data 테이블에서 key에 해당하는 JSON 데이터를 읽어온다. 없으면 None."""
    conn = get_db_connection()
    df = conn.query("select data from app_data where key = :key", params={"key": key}, ttl=0)
    if df.empty:
        return None
    return df.iloc[0]["data"]


def save_data(key, obj):
    """app_data 테이블에 key로 JSON 데이터를 저장(upsert)한다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into app_data (key, data, updated_at)
                values (:key, CAST(:data AS jsonb), now())
                on conflict (key) do update set data = excluded.data, updated_at = excluded.updated_at
                """
            ),
            {"key": key, "data": json.dumps(obj, ensure_ascii=False)},
        )
        session.commit()


def backup_data_before_overwrite(key, reason):
    """엑셀 업로드/태블로 동기화처럼 app_data(key)를 통째로 덮어쓰기 직전에, 지금 저장된
    데이터를 data_backups에 스냅샷으로 남긴다. 잘못된 파일을 올려도 직전 상태로 되돌릴 수
    있게 하기 위함. key별로 최근 30개만 남기고 오래된 백업은 지운다."""
    existing = load_data(key)
    if existing is None:
        return
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into data_backups (key, data, reason)
                values (:key, CAST(:data AS jsonb), :reason)
                """
            ),
            {"key": key, "data": json.dumps(existing, ensure_ascii=False), "reason": reason},
        )
        session.execute(
            text(
                """
                delete from data_backups
                where key = :key
                  and id not in (
                      select id from data_backups where key = :key order by backed_up_at desc, id desc limit 30
                  )
                """
            ),
            {"key": key},
        )
        session.commit()


@st.cache_data(ttl=30)
def load_camp_tab_permissions():
    """일반 캠프 로그인이 볼 수 있는 탭 키 목록 (물류창고는 별도, load_warehouse_tab_permissions).
    없으면 기본값(재고이관 2개). 로그인한 사람이 뭘 누르든 매 rerun마다 도는 조회라 짧게 캐시한다."""
    conn = get_db_connection()
    df = conn.query("select data from app_data where key = 'camp_tab_permissions'", ttl=0)
    if not df.empty:
        perms = df.iloc[0]["data"]
        if isinstance(perms, list) and perms:
            return perms
    return ["rebalance", "purchase"]


def save_camp_tab_permissions(keys):
    save_data("camp_tab_permissions", keys)


@st.cache_data(ttl=30)
def load_warehouse_tab_permissions():
    """물류창고 로그인이 볼 수 있는 탭 키 목록. 일반 캠프와 권한을 따로 관리하려고 분리함.
    없으면 기본값(재고이관(창고<>캠프), 창고 현황)."""
    conn = get_db_connection()
    df = conn.query("select data from app_data where key = 'warehouse_tab_permissions'", ttl=0)
    if not df.empty:
        perms = df.iloc[0]["data"]
        if isinstance(perms, list) and perms:
            return perms
    return ["purchase", "warehouse"]


def save_warehouse_tab_permissions(keys):
    save_data("warehouse_tab_permissions", keys)
