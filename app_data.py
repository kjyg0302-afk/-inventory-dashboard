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
        updated_at = session.execute(
            text(
                """
                insert into app_data (key, data, updated_at)
                values (:key, CAST(:data AS jsonb), now())
                on conflict (key) do update set data = excluded.data, updated_at = excluded.updated_at
                returning updated_at
                """
            ),
            {"key": key, "data": json.dumps(obj, ensure_ascii=False)},
        ).scalar()
        session.commit()
    # 공유 사본을 쓰는 키면 방금 저장한 것으로 바꿔 둔다 (DB에서 다시 읽지 않게).
    _blob_version.clear()
    if key in _blob_store():
        _blob_store()[key] = (updated_at, obj)


# ---------------- 서버 전체가 같이 쓰는 큰 데이터(재고·사용량) ----------------
# 예전엔 접속(세션)마다 재고·사용량 JSON을 DB에서 따로 읽어 각자 들고 있었다. 접속마다 수 초씩 CPU를
# 쓰고(2026-11-04 클라우드 로딩 멈춤의 원인), 로그인 시점 사본이라 다른 사람이 옮긴 재고가 안 보이고,
# 그 낡은 사본으로 입고 확정을 하면 최신 재고를 덮어쓸 위험도 있었다. 이제 서버에 사본 하나만 두고
# DB의 updated_at이 바뀌면(다른 서버·스크립트가 저장) 다시 읽는다.

@st.cache_resource
def _blob_store():
    """key -> (updated_at, data)"""
    return {}


@st.cache_data(ttl=20, show_spinner=False)
def _blob_version(key):
    conn = get_db_connection()
    with conn.session as session:
        return session.execute(text("select updated_at from app_data where key = :k"), {"k": key}).scalar()


def load_shared(key):
    """재고·사용량처럼 큰 데이터를 서버 공용 사본으로 돌려준다. 재고 이동처럼 고친 뒤에는 꼭 save_data로 저장할 것."""
    store = _blob_store()
    version = _blob_version(key)
    hit = store.get(key)
    if hit is None or hit[0] != version:
        hit = (version, load_data(key))
        store[key] = hit
    return hit[1]


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
