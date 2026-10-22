"""박스히어로(물류창고) 연동.
박스히어로는 캠프와는 별개인 물류창고 시스템. 재고 수량/출고 이력만 제공한다."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from db import get_db_connection

BOXHERO_API_BASE = "https://rest.boxhero-app.com/v1"


def get_boxhero_token():
    try:
        return st.secrets["boxhero"]["api_token"]
    except Exception:
        return None


def boxhero_get(path, params=None):
    token = get_boxhero_token()
    resp = requests.get(
        f"{BOXHERO_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def boxhero_paginate(path, params=None, max_pages=50):
    """cursor 기반 페이지네이션을 모두 순회해 items를 합쳐서 반환.
    박스히어로 자체 응답이 페이지당 0.7~1초 이상 걸려서 이미 초당 5회 제한보다 한참
    느리기 때문에, 추가로 대기를 넣지 않는다 (예전엔 0.25초씩 더 기다렸는데 순수 낭비였음)."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    all_items = []
    for i in range(max_pages):
        data = boxhero_get(path, params)
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        params["cursor"] = data["cursor"]
    return all_items


@st.cache_data(ttl=900, show_spinner=False)
def fetch_boxhero_locations():
    return boxhero_paginate("/locations")


@st.cache_data(ttl=900, show_spinner=False)
def fetch_boxhero_items():
    # 856개 기준 페이지네이션에 8초 가까이 걸려서, 캐시 만료 주기를 5분 -> 15분으로
    # 늘려 그 비용을 덜 자주 물게 한다 (물류창고 실물 재고가 몇 분 단위로 바뀌진 않아서
    # 신선도를 조금 희생해도 괜찮은 트레이드오프).
    return boxhero_paginate("/items")


@st.cache_data(ttl=900, show_spinner=False)
def get_boxhero_name_map():
    """SKU -> 박스히어로 품명 매핑. 박스히어로를 품명의 1차 기준으로 삼아서, 재고(태블로/엑셀)와
    사용량(엑셀) 쪽 품명이 서로 다르게 적혀 있어도 저장 시점에 이 매핑으로 통일한다."""
    if not get_boxhero_token():
        return {}
    try:
        items = fetch_boxhero_items()
    except Exception:
        return {}
    return {it["sku"].strip().upper(): it["name"] for it in items if it.get("sku") and it.get("name")}


def apply_boxhero_names_to_inventory(parsed):
    """parse_inventory_excel/fetch_tableau_inventory 결과의 품명을 박스히어로 기준으로 덮어쓴다."""
    name_map = get_boxhero_name_map()
    if name_map:
        for it in parsed["items"]:
            code = str(it.get("c") or "").strip().upper()
            if code in name_map:
                it["n"] = name_map[code]
    return parsed


def apply_boxhero_names_to_usage(parsed_usage, facts_df=None):
    """parse_usage_excel 결과(skuNames)와 facts_df의 품명을 박스히어로 기준으로 덮어쓴다."""
    name_map = get_boxhero_name_map()
    if name_map:
        sku_names = parsed_usage.get("skuNames") or {}
        # 기존에 이름이 있던 SKU뿐 아니라, 사용량만 있고 이름이 전혀 없던 SKU(원본 정비내역
        # 엑셀 경로처럼 품명 컬럼을 안 가져온 경우)도 박스히어로 기준으로 채운다.
        all_codes = set(sku_names.keys()) | set((parsed_usage.get("items") or {}).keys())
        for code in all_codes:
            code_u = str(code).strip().upper()
            if code_u in name_map:
                sku_names[code] = name_map[code_u]
        parsed_usage["skuNames"] = sku_names
        if facts_df is not None and not facts_df.empty:
            facts_df["item_name"] = facts_df["item_code"].astype(str).str.strip().str.upper().map(name_map).combine_first(
                facts_df["item_name"]
            )
    return parsed_usage, facts_df


_BOXHERO_MIN_GAP = 0.22  # 호출 간 최소 간격(초) — 박스히어로 제한(초당 5회) 아래로 유지


def _fetch_tx_details(tx_ids, token, workers=6):
    """출고 건 상세를 여러 개 동시에 조회한다. 건당 응답이 1~2초라 하나씩 하면 너무 느려서
    (30일 400건 기준 10분) 동시에 요청하되, 전체 호출 간격은 제한 아래로 맞춘다."""
    lock = threading.Lock()
    last_call = [0.0]

    def get(tx_id):
        for attempt in range(4):
            with lock:
                wait = last_call[0] + _BOXHERO_MIN_GAP - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last_call[0] = time.monotonic()
            resp = requests.get(
                f"{BOXHERO_API_BASE}/transactions/{tx_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            if resp.status_code == 429:  # 제한에 걸리면 잠깐 쉬었다가 다시
                time.sleep(1 + attempt)
                continue
            resp.raise_for_status()
            return tx_id, resp.json()["item"]
        resp.raise_for_status()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(get, tx_ids))


def _load_cached_out_tx(tx_ids):
    """DB에 저장해둔 출고 건 상세 {tx_id: (revision, lines)}."""
    if not tx_ids:
        return {}
    conn = get_db_connection()
    df = conn.query(
        "select tx_id, revision, lines from boxhero_out_tx where tx_id = any(:ids)",
        params={"ids": list(tx_ids)},
        ttl=0,
    )
    return {int(r.tx_id): (r.revision, r.lines) for r in df.itertuples()}


def _save_out_tx(rows):
    conn = get_db_connection()
    engine = conn.session.get_bind()
    with engine.begin() as c:
        for row in rows:
            c.execute(
                text(
                    """
                    insert into boxhero_out_tx (tx_id, revision, transaction_time, memo, partner, lines)
                    values (:tx_id, :revision, :transaction_time, :memo, :partner, CAST(:lines AS jsonb))
                    on conflict (tx_id) do update set revision = excluded.revision, memo = excluded.memo,
                        partner = excluded.partner, lines = excluded.lines, fetched_at = now()
                    """
                ),
                {**row, "lines": json.dumps(row["lines"], ensure_ascii=False)},
            )


def list_out_summaries(since=None, max_pages=500):
    """출고 목록(품목 정보 없음)을 최신순으로. since(datetime, UTC)보다 오래된 건을 만나면 멈춘다."""
    summaries = []
    params = {"type": "out", "limit": 100}
    for _ in range(max_pages):
        data = boxhero_get("/transactions", params)
        stopped = False
        for tx in data.get("items", []):
            if since is not None:
                tx_time = datetime.fromisoformat(tx["transaction_time"].replace("Z", "+00:00"))
                if tx_time < since:
                    stopped = True
                    break
            summaries.append(tx)
        if stopped or not data.get("has_more"):
            break
        params["cursor"] = data["cursor"]
    return summaries


def _tx_row(tx, detail):
    return {
        "tx_id": tx["id"],
        "revision": tx.get("revision"),
        "transaction_time": tx["transaction_time"],
        "memo": tx.get("memo"),
        "partner": (tx.get("partner") or {}).get("name"),
        "lines": [
            {
                "item_id": ln["item"]["id"],
                "sku": ln.get("sku") or ln["item"].get("sku"),
                "name": ln.get("name") or ln["item"].get("name"),
                "qty": abs(ln.get("quantity", 0)),
            }
            for ln in detail.get("items", [])
        ],
    }


def store_out_tx_details(summaries, batch=50, progress=None):
    """아직 저장 안 됐거나 revision이 바뀐 출고 건만 상세를 받아 boxhero_out_tx에 저장.
    batch건씩 받아서 바로 저장하므로 중간에 끊겨도 받은 만큼은 남는다. 반환: 새로 저장한 건수."""
    cached = _load_cached_out_tx([tx["id"] for tx in summaries])
    need = [tx for tx in summaries if tx["id"] not in cached or cached[tx["id"]][0] != tx.get("revision")]
    token = get_boxhero_token()
    for start in range(0, len(need), batch):
        chunk = need[start:start + batch]
        details = _fetch_tx_details([tx["id"] for tx in chunk], token)
        _save_out_tx([_tx_row(tx, details[tx["id"]]) for tx in chunk])
        if progress:
            progress(start + len(chunk), len(need))
    return len(need)


def sync_boxhero_out_tx(recheck_days=7):
    """마지막으로 저장된 출고 이후의 새 출고 건만 받아 저장 (최근 recheck_days일은 수정됐을 수
    있어서 revision을 다시 확인). 저장된 게 하나도 없으면 전체 이력을 받는다 — 오래 걸림."""
    conn = get_db_connection()
    latest = conn.query("select max(transaction_time) as t from boxhero_out_tx", ttl=0).iloc[0]["t"]
    since = None if latest is None or pd.isna(latest) else latest.to_pydatetime() - timedelta(days=recheck_days)
    n = store_out_tx_details(list_out_summaries(since=since))
    load_out_lines.clear()
    return n


@st.cache_data(ttl=300, show_spinner=False)
def load_out_lines(days=None):
    """저장된 출고 이력을 품목 라인 단위 DataFrame으로 (days=None이면 전체).
    columns: tx_id, transaction_time, memo, partner, item_id, sku, name, qty"""
    conn = get_db_connection()
    if days is None:
        df = conn.query("select tx_id, transaction_time, memo, partner, lines from boxhero_out_tx", ttl=0)
    else:
        df = conn.query(
            "select tx_id, transaction_time, memo, partner, lines from boxhero_out_tx "
            "where transaction_time >= now() - make_interval(days => :d)",
            params={"d": int(days)},
            ttl=0,
        )
    cols = ["tx_id", "transaction_time", "memo", "partner", "item_id", "sku", "name", "qty"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df.explode("lines").dropna(subset=["lines"]).reset_index(drop=True)
    lines = pd.json_normalize(df["lines"].tolist())
    out = pd.concat([df.drop(columns=["lines"]), lines], axis=1)
    return out[cols]


def last_out_tx_time():
    conn = get_db_connection()
    return conn.query("select max(transaction_time) as t, count(*) as n from boxhero_out_tx", ttl=0).iloc[0]
