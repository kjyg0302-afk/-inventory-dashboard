"""태블로 재고 데이터 자동 동기화.
"재고 내역_ver2" 뷰는 태블로 REST API로 롱 포맷(품목x캠프x측정값 1행씩) CSV를 내려준다.
parse_inventory_excel이 만드는 것과 같은 내부 구조로 피벗해서 맞춰준다."""

from datetime import datetime

import pandas as pd
import requests
import streamlit as st

TABLEAU_API_VERSION = "3.24"


def get_tableau_config():
    try:
        return st.secrets["tableau"]
    except Exception:
        return None


def get_tableau_session():
    """태블로에 로그인해 (auth_token, site_id)를 세션 동안 캐시해서 반환."""
    if "tableau_auth" in st.session_state:
        return st.session_state["tableau_auth"]
    cfg = get_tableau_config()
    if not cfg:
        return None
    resp = requests.post(
        f"{cfg['server']}/api/{TABLEAU_API_VERSION}/auth/signin",
        json={
            "credentials": {
                "personalAccessTokenName": cfg["token_name"],
                "personalAccessTokenSecret": cfg["token_secret"],
                "site": {"contentUrl": cfg["site"]},
            }
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    cred = resp.json()["credentials"]
    auth = (cred["token"], cred["site"]["id"])
    st.session_state["tableau_auth"] = auth
    return auth


def parse_tableau_inventory(csv_bytes) -> dict:
    """태블로 '재고 내역_ver2' 뷰의 롱 포맷 CSV를 parse_inventory_excel과 같은 내부 구조로 변환."""
    import io

    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    df["Measure Values"] = (
        df["Measure Values"].astype(str).str.replace(",", "", regex=False).astype(float)
    )
    # 태블로 뷰에 "전체 합계" 옵션이 켜져 있어, 부품/캠프/센터 모두 "All"인 합계용 가짜 행이 섞여 나온다.
    for col in ("부품 번호", "부품명", "캠프", "센터"):
        df = df[df[col].astype(str).str.strip() != "All"]

    teams, camps_order, camp_to_team = [], [], {}
    for _, r in df[["센터", "캠프"]].drop_duplicates().iterrows():
        team, camp = str(r["센터"]).strip(), str(r["캠프"]).strip()
        if team not in teams:
            teams.append(team)
        if camp not in camps_order:
            camps_order.append(camp)
            camp_to_team[camp] = team

    if not camps_order:
        raise ValueError("캠프/팀 정보를 찾을 수 없습니다. 태블로 뷰 구조가 바뀌었는지 확인해주세요.")

    pivot = df.pivot_table(
        index=["부품 번호", "부품명", "캠프"],
        columns="Measure Names",
        values="Measure Values",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()
    for col in ("재고 수량", "재고 금액"):
        if col not in pivot.columns:
            pivot[col] = 0

    items = []
    for (code, name), g in pivot.groupby(["부품 번호", "부품명"], sort=False):
        tot_qty = int(g["재고 수량"].sum())
        tot_amt = int(g["재고 금액"].sum())
        camps = {}
        for _, r in g.iterrows():
            q, a = int(r["재고 수량"]), int(r["재고 금액"])
            if q != 0 or a != 0:
                camps[str(r["캠프"]).strip()] = [q, a]
        items.append(
            {"n": str(name).strip(), "c": "" if pd.isna(code) else str(code).strip(), "q": tot_qty, "a": tot_amt, "x": camps}
        )
    items.sort(key=lambda it: it["n"])

    if not items:
        raise ValueError("품목 데이터를 찾을 수 없습니다.")

    return {
        "updatedAt": datetime.now().isoformat(),
        "teams": teams,
        "campToTeam": camp_to_team,
        "campsOrder": camps_order,
        "items": items,
    }


def fetch_tableau_inventory():
    """태블로에서 최신 재고 뷰 데이터를 받아와 내부 구조로 변환해 반환. 토큰이 만료됐으면 한 번 재로그인한다."""
    cfg = get_tableau_config()
    if not cfg:
        raise RuntimeError("태블로 연동 정보(.streamlit/secrets.toml의 [tableau])가 없습니다.")

    for attempt in range(2):
        auth = get_tableau_session()
        token, site_id = auth
        resp = requests.get(
            f"{cfg['server']}/api/{TABLEAU_API_VERSION}/sites/{site_id}/views/{cfg['inventory_view_id']}/data",
            headers={"X-Tableau-Auth": token},
            timeout=90,
        )
        if resp.status_code == 401 and attempt == 0:
            st.session_state.pop("tableau_auth", None)
            continue
        resp.raise_for_status()
        return parse_tableau_inventory(resp.content)
