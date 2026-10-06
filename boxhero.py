"""박스히어로(물류창고) 연동.
박스히어로는 캠프와는 별개인 물류창고 시스템. 재고 수량/출고 이력만 제공한다."""

import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

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
        for code in list(sku_names.keys()):
            code_u = str(code).strip().upper()
            if code_u in name_map:
                sku_names[code] = name_map[code_u]
        parsed_usage["skuNames"] = sku_names
        if facts_df is not None and not facts_df.empty:
            facts_df["item_name"] = facts_df["item_code"].astype(str).str.strip().str.upper().map(name_map).combine_first(
                facts_df["item_name"]
            )
    return parsed_usage, facts_df


@st.cache_data(ttl=600, show_spinner=False)
def fetch_boxhero_recent_out_transactions(days=30):
    """최근 days일 이내의 출고 트랜잭션을 가져온다 (최신순이라 기간을 벗어나면 즉시 중단).

    목록 API는 품목별 상세가 없어, 건별로 상세를 한 번씩 더 호출해 판매가 기준
    출고 금액(amt)을 계산해 붙인다. 건수가 많으면 다소 시간이 걸릴 수 있다.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    summaries = []
    params = {"type": "out", "limit": 100}
    for i in range(50):
        if i > 0:
            time.sleep(0.25)
        data = boxhero_get("/transactions", params)
        stopped = False
        for tx in data.get("items", []):
            tx_time = datetime.fromisoformat(tx["transaction_time"].replace("Z", "+00:00"))
            if tx_time < cutoff:
                stopped = True
                break
            summaries.append(tx)
        if stopped or not data.get("has_more"):
            break
        params["cursor"] = data["cursor"]

    price_by_id = {it["id"]: float(it.get("price") or 0) for it in fetch_boxhero_items()}

    results = []
    for i, tx in enumerate(summaries):
        if i > 0:
            time.sleep(0.2)
        detail = boxhero_get(f"/transactions/{tx['id']}")["item"]
        amt = sum(
            abs(line.get("quantity", 0)) * price_by_id.get(line["item"]["id"], 0)
            for line in detail.get("items", [])
        )
        results.append({**tx, "amt": amt})
    return results
