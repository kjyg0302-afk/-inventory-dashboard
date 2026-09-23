"""
지바이크 SCM 대시보드 (Streamlit)
- 태블로에서 받은 재고 엑셀(피벗 형식)과, scm_data.db에서 export_weekly_usage.py로
  뽑은 주단위 사용량 JSON(또는 사용량 엑셀)을 업로드하면 대시보드가 채워집니다.
- 업로드한 데이터는 Supabase(Postgres) DB에 저장되어, 다시 접속하는 모든 사람에게
  그대로 보입니다. 앱이 잠들었다 깨어나거나 재배포되어도 DB에 저장된 데이터는
  유지됩니다. (연결 설정은 .streamlit/secrets.toml.example, schema.sql 참고)
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="지바이크 SCM 대시보드", layout="wide")

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}
    .block-container {padding-top: 2rem; padding-bottom: 3rem; max-width: 1300px;}
    </style>
    """,
    unsafe_allow_html=True,
)

DB_CONN_NAME = "supabase_db"


# ---------------- 데이터 파싱 ----------------

# 사용량 엑셀의 캠프명과 재고 엑셀의 캠프명이 다르게 기록된 경우 합쳐주는 규칙.
# (export_weekly_usage.py의 CAMP_NAME_MAP과 동일하게 유지)
USAGE_CAMP_NAME_MAP = {
    "부산캠프": "부산2캠프",
    "부산정비": "부산2캠프",
    "광주정비": "광주1캠프",
    "대구정비": "대구1캠프",
    "고양1정비": "고양1캠프",
    "중앙정비허브": "천안캠프",
    # 사용량 원본 엑셀에 섞여 있는 오타 보정
    "서초캐프": "서초캠프",
    "서초켐프": "서초캠프",
    "고양2캠": "고양2캠프",
}
USAGE_EXCLUDE_CAMPS = {"김만수(가맹임대)영천"}


def parse_usage_excel(file) -> dict:
    """캠프별 사용량 엑셀(태블로 내보내기, 년도/해당주/고객명/부품번호별 로우 데이터)을
    읽어 usage.json과 같은 구조로 변환."""
    df = pd.read_excel(file, header=1)

    required = {"년도", "월", "해당주", "고객명", "부품번호", "합계 : 수량", "합계 : 부품계"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"사용량 엑셀 형식이 올바르지 않습니다. 다음 컬럼이 없습니다: {', '.join(missing)}"
        )

    # 부품번호가 없는 행(공임/입고/점검 등 재고와 무관한 항목, 소계/총합계 행)은 제외
    df = df[df["부품번호"].notna() & (df["부품번호"].astype(str).str.strip() != "(비어 있음)")].copy()
    if df.empty:
        raise ValueError("부품번호가 있는 사용량 데이터를 찾을 수 없습니다.")

    df["년도"] = pd.to_numeric(df["년도"], errors="coerce")
    df["월"] = pd.to_numeric(df["월"], errors="coerce")
    df["해당주"] = pd.to_numeric(df["해당주"], errors="coerce")
    df = df.dropna(subset=["년도", "월", "해당주"])
    df["년도"] = df["년도"].astype(int)
    df["월"] = df["월"].astype(int)
    df["해당주"] = df["해당주"].astype(int)

    df["고객명"] = df["고객명"].astype(str).str.strip().map(lambda c: USAGE_CAMP_NAME_MAP.get(c, c))
    df = df[~df["고객명"].isin(USAGE_EXCLUDE_CAMPS)]
    # 캠프명이 깨져서 숫자만 들어간 오염된 행 제외
    df = df[~df["고객명"].str.fullmatch(r"\d+")]

    df["부품번호"] = df["부품번호"].astype(str).str.strip()
    df["합계 : 수량"] = pd.to_numeric(df["합계 : 수량"], errors="coerce").fillna(0)
    df["합계 : 부품계"] = pd.to_numeric(df["합계 : 부품계"], errors="coerce").fillna(0)

    camp_week_count = (
        df[["고객명", "년도", "해당주"]].drop_duplicates().groupby("고객명").size().to_dict()
    )

    agg = df.groupby(["부품번호", "고객명", "년도", "해당주"])["합계 : 수량"].sum()

    by_part = {}
    for (part_no, camp, year, week), qty in agg.items():
        if qty == 0:
            continue
        by_part.setdefault(part_no, {}).setdefault(camp, []).append([int(year), int(week), float(qty)])

    items = {}
    for part_no, camps in by_part.items():
        camp_out = {}
        for camp, wlist in camps.items():
            wlist.sort(key=lambda w: (w[0], w[1]))
            total = sum(w[2] for w in wlist)
            denom = camp_week_count.get(camp, 1) or 1
            camp_out[camp] = {"w": wlist, "t": round(total, 2), "avg": round(total / denom, 2)}
        items[part_no] = camp_out

    if not items:
        raise ValueError("집계할 수 있는 사용량 데이터를 찾을 수 없습니다.")

    all_weeks = sorted({(y, w) for (_, _, y, w) in agg.index})

    # 월별 x 캠프별 사용 금액 집계 ("YYYY-MM" -> {캠프: 금액})
    monthly_amt = df.groupby(["년도", "월", "고객명"])["합계 : 부품계"].sum()
    monthly_camp_amount = {}
    for (year, month, camp), amt in monthly_amt.items():
        key = f"{int(year)}-{int(month):02d}"
        monthly_camp_amount.setdefault(key, {})[camp] = round(float(amt), 2)

    # 월별 x 부품번호(SKU)별 사용 금액 집계 ("YYYY-MM" -> {부품번호: 금액})
    monthly_sku_amt = df.groupby(["년도", "월", "부품번호"])["합계 : 부품계"].sum()
    monthly_sku_amount = {}
    for (year, month, sku), amt in monthly_sku_amt.items():
        key = f"{int(year)}-{int(month):02d}"
        monthly_sku_amount.setdefault(key, {})[sku] = round(float(amt), 2)

    # 부품번호 -> 품명 (가장 최근 값 사용)
    sku_names = {}
    if "부품" in df.columns:
        sku_names = (
            df.dropna(subset=["부품"])
            .groupby("부품번호")["부품"]
            .last()
            .to_dict()
        )

    # 캠프별 주 평균 사용 금액 (전체 기간 총 사용 금액 ÷ 그 캠프의 실제 데이터 존재 주 수)
    camp_total_amt = df.groupby("고객명")["합계 : 부품계"].sum()
    camp_weekly_amount = {
        camp: round(float(total) / (camp_week_count.get(camp, 1) or 1), 2)
        for camp, total in camp_total_amt.items()
    }

    # 월별 x 부품번호 x 캠프별 사용 수량 집계 (부품번호 -> "YYYY-MM" -> {캠프: 수량})
    monthly_qty = df.groupby(["년도", "월", "부품번호", "고객명"])["합계 : 수량"].sum()
    monthly_item_camp_qty = {}
    for (year, month, part_no, camp), qty in monthly_qty.items():
        if qty == 0:
            continue
        key = f"{int(year)}-{int(month):02d}"
        monthly_item_camp_qty.setdefault(part_no, {}).setdefault(key, {})[camp] = float(qty)

    # 부품번호별 주 평균 사용 금액 (전체 기간 총 사용 금액 ÷ 그 부품이 실제 사용된 주 수)
    sku_week_counts = df[["부품번호", "년도", "해당주"]].drop_duplicates().groupby("부품번호").size()
    sku_total_amt = df.groupby("부품번호")["합계 : 부품계"].sum()
    sku_weekly_amount = {
        sku: round(float(total) / (sku_week_counts.get(sku, 1) or 1), 2)
        for sku, total in sku_total_amt.items()
    }

    # 로우 단위 사실 테이블 (usage_facts DB 테이블용) — 부품×캠프×연도×월×주 단위 수량/금액.
    # usage.json 요약과 달리 미리 정해둔 모양이 없어, SQL로 자유롭게 재집계하거나 예측에 바로 쓸 수 있다.
    facts_df = df.groupby(["부품번호", "고객명", "년도", "월", "해당주"], as_index=False).agg(
        qty=("합계 : 수량", "sum"), amt=("합계 : 부품계", "sum")
    )
    facts_df["item_name"] = facts_df["부품번호"].map(sku_names)
    facts_df = facts_df.rename(
        columns={"부품번호": "item_code", "고객명": "camp", "년도": "year", "월": "month", "해당주": "week"}
    )
    facts_df = facts_df[["year", "month", "week", "camp", "item_code", "item_name", "qty", "amt"]]

    return {
        "updatedAt": datetime.now().isoformat(),
        "campWeekCount": camp_week_count,
        "weekRange": {
            "from": list(all_weeks[0]) if all_weeks else None,
            "to": list(all_weeks[-1]) if all_weeks else None,
            "count": len(all_weeks),
        },
        "items": items,
        "monthlyCampAmount": monthly_camp_amount,
        "monthlySkuAmount": monthly_sku_amount,
        "skuNames": sku_names,
        "campWeeklyAmount": camp_weekly_amount,
        "monthlyItemCampQty": monthly_item_camp_qty,
        "skuWeeklyAmount": sku_weekly_amount,
        "facts": facts_df,
    }


def parse_inventory_excel(file) -> dict:
    """태블로 재고 내역 엑셀(피벗 구조)을 읽어 내부 JSON 구조로 변환."""
    df = pd.read_excel(file, header=None)
    team_row = df.iloc[0].tolist()
    camp_row = df.iloc[1].tolist()
    width = max(len(team_row), len(camp_row))

    camps_order, camp_to_team, teams = [], {}, []
    cur_team = None
    for i in range(4, width, 2):
        t = team_row[i] if i < len(team_row) else None
        c = camp_row[i] if i < len(camp_row) else None
        if isinstance(t, str) and t.strip():
            cur_team = t
            if t not in teams:
                teams.append(t)
        if isinstance(c, str) and c.strip():
            camps_order.append(c)
            camp_to_team[c] = cur_team

    if not camps_order:
        raise ValueError("캠프/팀 헤더를 찾을 수 없습니다. 원본 태블로 내보내기 형식인지 확인해주세요.")

    items = []
    for r in range(3, len(df)):
        row = df.iloc[r]
        name = row[0]
        if pd.isna(name) or str(name).strip() in ("", "총합계"):
            continue
        code = row[1]
        code = "" if pd.isna(code) else str(code).strip()
        tot_qty = 0 if pd.isna(row[2]) else int(row[2])
        tot_amt = 0 if pd.isna(row[3]) else int(row[3])
        camps = {}
        ci = 4
        for camp in camps_order:
            q = row[ci] if ci < len(row) else None
            a = row[ci + 1] if ci + 1 < len(row) else None
            q = 0 if pd.isna(q) else int(q)
            a = 0 if pd.isna(a) else int(a)
            if q != 0 or a != 0:
                camps[camp] = [q, a]
            ci += 2
        items.append({"n": str(name), "c": code, "q": tot_qty, "a": tot_amt, "x": camps})

    if not items:
        raise ValueError("품목 데이터를 찾을 수 없습니다.")

    return {
        "updatedAt": datetime.now().isoformat(),
        "teams": teams,
        "campToTeam": camp_to_team,
        "campsOrder": camps_order,
        "items": items,
    }


# ---------------- 태블로 재고 데이터 자동 동기화 ----------------
# "재고 내역_ver2" 뷰는 태블로 REST API로 롱 포맷(품목x캠프x측정값 1행씩) CSV를 내려준다.
# parse_inventory_excel이 만드는 것과 같은 내부 구조로 피벗해서 맞춰준다.

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


def get_db_connection():
    try:
        return st.connection(DB_CONN_NAME, type="sql")
    except Exception as e:
        st.error(
            "DB 연결 정보를 찾을 수 없습니다. .streamlit/secrets.toml.example을 참고해 "
            "secrets.toml을 만들거나 Streamlit Cloud의 Secrets 설정에 등록해주세요.\n\n"
            f"({e})",
            icon=":material/error:",
        )
        st.stop()


def hash_password(password):
    salt = uuid.uuid4().hex
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(password, stored_hash):
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, digest = stored_hash.split("$", 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == digest


def list_camp_credential_names():
    conn = get_db_connection()
    df = conn.query("select camp from camp_credentials order by camp", ttl=30)
    return df["camp"].tolist()


def get_camp_password_hash(camp):
    conn = get_db_connection()
    df = conn.query(
        "select password_hash from camp_credentials where camp = :camp", params={"camp": camp}, ttl=0
    )
    return df.iloc[0]["password_hash"] if not df.empty else None


def set_camp_password(camp, password):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into camp_credentials (camp, password_hash) values (:camp, :hash)
                on conflict (camp) do update set password_hash = excluded.password_hash, updated_at = now()
                """
            ),
            {"camp": camp, "hash": hash_password(password)},
        )
        session.commit()


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


def save_usage_facts(facts_df):
    """usage_facts 테이블을 사용량 엑셀 기준으로 통째로 새로 채운다 (전체 교체)."""
    conn = get_db_connection()
    engine = conn.session.get_bind()
    with engine.begin() as connection:
        connection.execute(text("truncate table usage_facts"))
        facts_df.to_sql("usage_facts", con=connection, if_exists="append", index=False, method="multi", chunksize=1000)


def list_transfer_requests(item_code):
    """특정 품목의 이관 요청 이력을 최신순으로 반환."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where item_code = :item_code order by requested_at desc",
        params={"item_code": item_code},
        ttl=0,
    )


def list_pending_transfer_requests_for_camp(camp):
    """해당 캠프가 보내는 쪽으로 승인 대기 중인(=아직 처리 안 한) 이관 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where from_camp = :camp and status = 'requested' "
        "order by requested_at desc",
        params={"camp": camp},
        ttl=10,
    )


def create_transfer_request(item_code, item_name, from_camp, to_camp, qty, requested_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into transfer_requests (item_code, item_name, from_camp, to_camp, qty, requested_by)
                values (:item_code, :item_name, :from_camp, :to_camp, :qty, :requested_by)
                """
            ),
            {
                "item_code": item_code,
                "item_name": item_name,
                "from_camp": from_camp,
                "to_camp": to_camp,
                "qty": qty,
                "requested_by": requested_by,
            },
        )
        session.commit()


def approve_transfer_request(request_id, approved_by):
    """요청을 승인하고 이동중 상태로 전환. 실제 재고 차감은 입고완료 시점에 이루어진다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'in_transit', approved_by = :approved_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "approved_by": approved_by},
        )
        session.commit()


def reject_transfer_request(request_id, rejected_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'rejected', approved_by = :rejected_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": request_id, "rejected_by": rejected_by},
        )
        session.commit()


def complete_transfer_request(request_id, received_by, amt):
    """입고완료 처리. 실제 이동한 금액(amt)을 함께 기록한다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update transfer_requests
                set status = 'completed', received_by = :received_by, received_at = now(), amt = :amt
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": request_id, "received_by": received_by, "amt": amt},
        )
        session.commit()


def save_warehouse_snapshot(total_qty, total_amt, item_count):
    """오늘 날짜로 창고 재고 스냅샷을 저장(upsert)한다. 박스히어로 API가 현재 시점만
    알려주기 때문에, 창고 현황 탭을 열 때마다 오늘자 스냅샷을 남겨 월별 추이를 쌓는다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into warehouse_snapshots (snapshot_date, total_qty, total_amt, item_count)
                values (current_date, :qty, :amt, :cnt)
                on conflict (snapshot_date) do update
                set total_qty = excluded.total_qty, total_amt = excluded.total_amt,
                    item_count = excluded.item_count, created_at = now()
                """
            ),
            {"qty": total_qty, "amt": total_amt, "cnt": item_count},
        )
        session.commit()


def load_warehouse_snapshots():
    conn = get_db_connection()
    return conn.query("select * from warehouse_snapshots order by snapshot_date", ttl=60)


def current_inventory_period():
    """(ISO 연도, ISO 주차, 'YYYY-Www' 표시용 라벨)을 반환."""
    iso_year, iso_week, _ = datetime.now().isocalendar()
    return iso_year, iso_week, f"{iso_year}-W{iso_week:02d}"


def save_inventory_value_snapshot(source, rows):
    """rows: [{"camp","item_code","item_name","qty","unit_price","amt"}, ...]로 이번 주(ISO 연도+주차)
    현황을 채운다 (이번 주 + 해당 source의 기존 행만 지우고 새로 append — 지난 주들의 누적 이력은
    그대로 남는다). 같은 주 안에서 여러 번 호출되면 그 주 행만 최신 값으로 덮어쓰고, 주가 바뀌면
    새 행 묶음이 추가된다 (예: 2026-W38 재고현황 800행, 2026-W39 재고현황 805행, ...).
    qty와 그 시점 단가(unit_price)를 따로 남겨서, 나중에 단가가 바뀌어도 qty에 원하는 기준
    단가를 곱해 같은 기준으로 재고 금액을 다시 계산할 수 있다.
    수천 건 단위라 upsert보다 delete+bulk append가 훨씬 빠르다."""
    if not rows:
        return
    conn = get_db_connection()
    year, week, label = current_inventory_period()
    df = pd.DataFrame(rows)
    df["year"] = year
    df["week"] = week
    df["period_label"] = label
    df["snapshot_date"] = datetime.now().date()
    df["source"] = source
    engine = conn.session.get_bind()
    with engine.begin() as connection:
        connection.execute(
            text("delete from inventory_value_snapshots where year = :y and week = :w and source = :s"),
            {"y": year, "w": week, "s": source},
        )
        df.to_sql(
            "inventory_value_snapshots", con=connection, if_exists="append", index=False,
            method="multi", chunksize=1000,
        )


def build_camp_value_snapshot_rows(data):
    rows = []
    for it in data["items"]:
        code = it.get("c")
        if not code:
            continue
        for camp, (q, a) in it.get("x", {}).items():
            if q == 0 and a == 0:
                continue
            rows.append(
                {
                    "camp": camp,
                    "item_code": code,
                    "item_name": it["n"],
                    "qty": q,
                    "unit_price": (a / q) if q else None,
                    "amt": a,
                }
            )
    return rows


def build_warehouse_value_snapshot_rows(wh_items):
    rows = []
    for it in wh_items:
        sku = it.get("sku")
        if not sku:
            continue
        qty = it.get("quantity", 0)
        price = float(it.get("price") or 0)
        rows.append(
            {
                "camp": "",
                "item_code": sku,
                "item_name": it.get("name"),
                "qty": qty,
                "unit_price": price if qty else None,
                "amt": qty * price,
            }
        )
    return rows


def load_inventory_value_trend():
    """주 x 구분(캠프/창고)별 재고 금액 합계 추이 (스냅샷 당시 단가 기준)."""
    conn = get_db_connection()
    return conn.query(
        """
        select year, week, period_label, max(snapshot_date) as snapshot_date, source,
               sum(qty) as qty, sum(amt) as amt
        from inventory_value_snapshots
        group by year, week, period_label, source
        order by year, week
        """,
        ttl=60,
    )


def list_warehouse_orders():
    """전체 발주 요청 이력을 최신순으로 반환."""
    conn = get_db_connection()
    return conn.query("select * from warehouse_orders order by requested_at desc", ttl=0)


def list_pending_warehouse_orders():
    """물류창고가 승인해야 할, 아직 처리 안 한 발주 요청 목록."""
    conn = get_db_connection()
    return conn.query(
        "select * from warehouse_orders where status = 'requested' order by requested_at desc",
        ttl=10,
    )


def create_warehouse_order(item_code, item_name, to_camp, qty, weekly_avg_usage, reason, requested_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into warehouse_orders
                    (item_code, item_name, to_camp, qty, weekly_avg_usage, reason, requested_by)
                values (:item_code, :item_name, :to_camp, :qty, :weekly_avg_usage, :reason, :requested_by)
                """
            ),
            {
                "item_code": item_code,
                "item_name": item_name,
                "to_camp": to_camp,
                "qty": qty,
                "weekly_avg_usage": weekly_avg_usage,
                "reason": reason or None,
                "requested_by": requested_by,
            },
        )
        session.commit()


def approve_warehouse_order(order_id, approved_by):
    """발주를 승인 처리 (창고에서 발송 준비 단계로 전환). 박스히어로 실제 재고는 건드리지 않는다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'in_transit', approved_by = :approved_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": order_id, "approved_by": approved_by},
        )
        session.commit()


def reject_warehouse_order(order_id, rejected_by):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'rejected', approved_by = :rejected_by, approved_at = now()
                where id = :id and status = 'requested'
                """
            ),
            {"id": order_id, "rejected_by": rejected_by},
        )
        session.commit()


def complete_warehouse_order(order_id, received_by, amt):
    """입고완료 처리. 이때만 실제로 캠프 재고 데이터에 반영된다."""
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                update warehouse_orders
                set status = 'completed', received_by = :received_by, received_at = now(), amt = :amt
                where id = :id and status = 'in_transit'
                """
            ),
            {"id": order_id, "received_by": received_by, "amt": amt},
        )
        session.commit()


def camp_weekly_usage_rate(code, camp):
    """해당 SKU의 특정 캠프 기준 주 평균 사용량 (사용량 데이터 없으면 None)."""
    item_usage = get_usage_for_code(code)
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
        sql, params={"cur_year": now.year, "cur_month": now.month, "camp": camp, "limit": limit}, ttl=0
    )


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


def load_demand_forecasts(camp, year, month):
    conn = get_db_connection()
    return conn.query(
        "select * from demand_forecasts where camp = :camp and forecast_year = :year and forecast_month = :month",
        params={"camp": camp, "year": year, "month": month},
        ttl=0,
    )


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


def find_item_by_code(inventory, code):
    for it in inventory["items"]:
        if it["c"] == code:
            return it
    return None


def deduct_camp_stock(item, camp, qty):
    """캠프 재고에서 qty만큼 차감하고, 차감된 금액을 반환한다 (재고 부족 시 ValueError)."""
    pair = item["x"].get(camp)
    have = pair[0] if pair else 0
    if have < qty:
        raise ValueError(f"{camp}의 재고가 부족합니다. (보유 {have}개, 요청 {qty}개)")
    q, a = pair
    unit_amt = a / q if q else 0
    moved_amt = round(unit_amt * qty)
    new_q, new_a = q - qty, a - moved_amt
    if new_q <= 0:
        del item["x"][camp]
    else:
        item["x"][camp] = [new_q, new_a]
    return moved_amt


def add_camp_stock(item, camp, qty, amt):
    """캠프 재고에 qty/amt만큼 더한다 (해당 캠프에 재고가 없었다면 새로 만든다)."""
    pair = item["x"].get(camp)
    if pair:
        item["x"][camp] = [pair[0] + qty, pair[1] + amt]
    else:
        item["x"][camp] = [qty, amt]


def render_pending_request_row(r, data, my_name):
    """요청중 상태인 이관 요청 한 건을 표시하고 승인/거절 버튼을 처리한다.
    로그인 배너의 "내 요청함"과 재분배 도우미 탭 양쪽에서 재사용한다."""
    c0, c1, c2, c3 = st.columns([1, 4, 1, 1])
    c0.badge("요청중", icon=":material/schedule:", color="orange")
    c1.write(f"{r['item_name']} · {r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · 요청자: {r['requested_by']}")
    if c2.button("승인", key=f"approve_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            item = find_item_by_code(data, r["item_code"])
            current_qty = item["x"].get(r["from_camp"], [0, 0])[0] if item else 0
            req_df_all = list_transfer_requests(r["item_code"])
            already_out = int(
                req_df_all[
                    (req_df_all["status"] == "in_transit") & (req_df_all["from_camp"] == r["from_camp"])
                ]["qty"].sum()
            )
            available = current_qty - already_out
            if available < r["qty"]:
                st.error(
                    f"{r['from_camp']}의 가용재고가 부족합니다. (가용 {available}개, 요청 {int(r['qty'])}개)",
                    icon=":material/error:",
                )
            else:
                approve_transfer_request(r["id"], my_name.strip())
                st.success("승인했습니다. 이동중 상태로 전환됩니다.", icon=":material/task_alt:")
                st.rerun()
    if c3.button("거절", key=f"reject_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            reject_transfer_request(r["id"], my_name.strip())
            st.rerun()


def render_pending_warehouse_order_row(r, my_name):
    """요청중 상태인 발주 요청(캠프 -> 물류창고) 한 건을 표시하고 승인/거절 버튼을 처리한다."""
    c0, c1, c2 = st.columns([4, 1, 1])
    reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
    c0.write(
        f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
        f"요청자: {r['requested_by']}{reason_suffix}"
    )
    if c1.button("승인", key=f"wh_po_approve_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            approve_warehouse_order(r["id"], my_name.strip())
            st.rerun()
    if c2.button("거절", key=f"wh_po_reject_{r['id']}"):
        if not my_name.strip():
            st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
        else:
            reject_warehouse_order(r["id"], my_name.strip())
            st.rerun()


# ---------------- 박스히어로(물류창고) 연동 ----------------
# 박스히어로는 캠프와는 별개인 물류창고 시스템. 재고 수량/출고 이력만 제공한다.

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
    """cursor 기반 페이지네이션을 모두 순회해 items를 합쳐서 반환 (초당 5회 제한을 지키기 위해 살짝 대기)."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    all_items = []
    for i in range(max_pages):
        if i > 0:
            time.sleep(0.25)
        data = boxhero_get(path, params)
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        params["cursor"] = data["cursor"]
    return all_items


@st.cache_data(ttl=300, show_spinner=False)
def fetch_boxhero_locations():
    return boxhero_paginate("/locations")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_boxhero_items():
    return boxhero_paginate("/items")


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


def fmt_int(n):
    return f"{round(n or 0):,}"


def fmt_won(n):
    return f"₩{round(n or 0):,}"


def monthly_mean_excluding_current(monthly_series):
    """월별 금액 Series의 평균을 구하되, 아직 마감 전인 이번 달은 제외한다
    (이번 달만 있으면 왜곡을 막기 위해 전체 평균으로 대체)."""
    current_month = datetime.now().strftime("%Y-%m")
    completed = monthly_series.drop(index=current_month, errors="ignore")
    return completed.mean() if not completed.empty else monthly_series.mean()


ACCENT = "#5B8DEF"
CHART_GRID = "#20293A"
CHART_MUTED = "#8A96A8"


def render_trend_chart(df, x_col, y_col, height=220):
    """부드럽게 이어진 선 그래프 + 그라데이션 영역으로 추이를 그린다 (다크 테마 전용)."""
    base = alt.Chart(df).encode(
        x=alt.X(
            f"{x_col}:O",
            sort=None,
            title=None,
            axis=alt.Axis(
                labelColor=CHART_MUTED,
                labelFontSize=11,
                domain=False,
                ticks=False,
                grid=False,
            ),
        ),
        y=alt.Y(
            f"{y_col}:Q",
            title=None,
            axis=alt.Axis(
                labelColor=CHART_MUTED,
                labelFontSize=11,
                domain=False,
                ticks=False,
                gridColor=CHART_GRID,
                tickCount=4,
            ),
        ),
    )

    area = base.mark_area(
        interpolate="monotone",
        color=alt.Gradient(
            gradient="linear",
            stops=[
                alt.GradientStop(color=ACCENT, offset=0),
                alt.GradientStop(color=ACCENT, offset=1),
            ],
            x1=1, y1=1, x2=1, y2=0,
        ),
        opacity=0.18,
    )

    line = base.mark_line(
        interpolate="monotone",
        color=ACCENT,
        strokeWidth=2.5,
        point=alt.OverlayMarkDef(filled=True, fill=ACCENT, stroke="#10141B", strokeWidth=1.5, size=55),
    ).encode(
        tooltip=[
            alt.Tooltip(f"{x_col}:O", title=x_col),
            alt.Tooltip(f"{y_col}:Q", title=y_col, format=",.0f"),
        ]
    )

    chart = (
        (area + line)
        .properties(height=height)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    return chart


# ---------------- 로그인 ----------------
# 개인별 계정이 아니라, 캠프 하나당 비밀번호 하나를 공유하는 가벼운 방식. 관리자는 별도
# 마스터 비밀번호(secrets.toml)로 로그인해서 캠프 제한 없이 전체를 보고 캠프 비밀번호도 관리한다.

def render_login():
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:14px;margin:48px 0 28px;">
            <div style="width:42px;height:42px;border-radius:50%;flex-shrink:0;
                        background:linear-gradient(135deg, #6FA0FF, #4A6FE0);
                        box-shadow:0 4px 16px rgba(74,111,224,0.45);
                        display:flex;align-items:center;justify-content:center;">
                <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="white"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                    <line x1="12" y1="22.08" x2="12" y2="12"/>
                </svg>
            </div>
            <div style="font-size:21px;font-weight:700;letter-spacing:-0.01em;">지바이크 SCM 대시보드</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    login_mode = st.radio("로그인 방식", ["캠프로 로그인", "관리자로 로그인"], horizontal=True)
    with st.form("login_form"):
        if login_mode == "캠프로 로그인":
            camps = list_camp_credential_names()
            camp = st.selectbox("캠프", camps) if camps else None
            pin = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
            if submitted:
                if not camps:
                    st.error("등록된 캠프 계정이 없어요. 관리자에게 문의해주세요.", icon=":material/error:")
                elif verify_password(pin, get_camp_password_hash(camp)):
                    st.session_state["auth"] = {"role": "camp", "camp": camp}
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")
        else:
            admin_pw = st.text_input("관리자 비밀번호", type="password")
            submitted = st.form_submit_button("로그인", type="primary", icon=":material/login:")
            if submitted:
                try:
                    correct = st.secrets["admin"]["password"]
                except Exception:
                    correct = None
                if correct and admin_pw == correct:
                    st.session_state["auth"] = {"role": "admin", "camp": None}
                    st.rerun()
                else:
                    st.error("비밀번호가 올바르지 않습니다.", icon=":material/error:")


if not st.session_state.get("auth"):
    render_login()
    st.stop()


# ---------------- 세션 상태 로드 ----------------

if "inventory" not in st.session_state:
    st.session_state.inventory = load_data("inventory")
if "usage" not in st.session_state:
    st.session_state.usage = load_data("usage")

data = st.session_state.inventory
usage = st.session_state.usage


# ---------------- 헤더 ----------------

col_title, col_upload1, col_upload2 = st.columns([3, 1, 1])
with col_title:
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:2px;">
            <div style="width:42px;height:42px;border-radius:50%;flex-shrink:0;
                        background:linear-gradient(135deg, #6FA0FF, #4A6FE0);
                        box-shadow:0 4px 16px rgba(74,111,224,0.45);
                        display:flex;align-items:center;justify-content:center;">
                <svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="white"
                     stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
                    <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
                    <line x1="12" y1="22.08" x2="12" y2="12"/>
                </svg>
            </div>
            <div style="font-size:21px;font-weight:700;letter-spacing:-0.01em;">지바이크 SCM 대시보드</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    updated = data["updatedAt"][:16].replace("T", " ") if data else "-"
    st.caption(f"전 캠프 재고 데이터 · 마지막 업데이트 {updated}")

with col_upload1:
    inv_file = st.file_uploader("재고 엑셀로 갱신", type=["xlsx", "xls"], key="inv_upload")
    # file_uploader는 업로드된 파일을 계속 들고 있어서, 다른 상호작용으로 스크립트가
    # 재실행될 때마다 이 블록이 다시 돌지 않도록 이미 처리한 파일인지 확인한다.
    if inv_file is not None and st.session_state.get("_inv_file_id") != inv_file.file_id:
        try:
            parsed = parse_inventory_excel(inv_file)
            save_data("inventory", parsed)
            st.session_state.inventory = parsed
            data = parsed
            st.session_state["_inv_file_id"] = inv_file.file_id
            st.success(f"업데이트 완료 · 품목 {len(parsed['items']):,}개 · 캠프 {len(parsed['campsOrder'])}곳", icon=":material/check_circle:")
        except Exception as e:
            st.error(f"파일을 읽는 중 문제가 발생했습니다: {e}", icon=":material/error:")

    if get_tableau_config():
        if st.button("태블로에서 바로 동기화", icon=":material/sync:", key="tableau_sync_btn"):
            try:
                with st.spinner("태블로에서 재고 데이터를 받아오는 중이에요..."):
                    parsed = fetch_tableau_inventory()
                save_data("inventory", parsed)
                st.session_state.inventory = parsed
                data = parsed
                st.success(f"동기화 완료 · 품목 {len(parsed['items']):,}개 · 캠프 {len(parsed['campsOrder'])}곳", icon=":material/check_circle:")
            except Exception as e:
                st.error(f"태블로 동기화 중 문제가 발생했습니다: {e}", icon=":material/error:")

with col_upload2:
    usage_file = st.file_uploader(
        "사용량 데이터 갱신 (엑셀 또는 JSON)", type=["xlsx", "xls", "json"], key="usage_upload"
    )
    if usage_file is not None and st.session_state.get("_usage_file_id") != usage_file.file_id:
        try:
            with st.spinner("사용량 데이터를 처리하는 중이에요... (대용량 엑셀은 시간이 걸릴 수 있어요)"):
                if usage_file.name.lower().endswith(".json"):
                    parsed_usage = json.load(usage_file)
                    if "items" not in parsed_usage:
                        raise ValueError("사용량 JSON 형식이 올바르지 않습니다.")
                else:
                    parsed_usage = parse_usage_excel(usage_file)
                    facts_df = parsed_usage.pop("facts")
                    save_usage_facts(facts_df)
                save_data("usage", parsed_usage)
            st.session_state.usage = parsed_usage
            usage = parsed_usage
            st.session_state["_usage_file_id"] = usage_file.file_id
            st.success(f"사용량 갱신 완료 · 부품 {len(parsed_usage['items']):,}종", icon=":material/check_circle:")
        except Exception as e:
            st.error(f"사용량 파일을 읽는 중 문제가 발생했습니다: {e}", icon=":material/error:")

if usage and usage.get("weekRange"):
    wr = usage["weekRange"]
    st.info(f"사용량 데이터 기준: {wr['from'][0]}-{wr['from'][1]}주 ~ {wr['to'][0]}-{wr['to'][1]}주 ({wr['count']}주)", icon=":material/insights:")

st.divider()

if not data:
    st.warning("아직 업로드된 재고 데이터가 없어요. 위에서 재고 엑셀을 업로드해주세요.", icon=":material/upload_file:")
    st.stop()


# ---------------- 로그인 상태 / 내 캠프 알림 ----------------

_login_accounts = data["campsOrder"] + ["물류창고"]

auth = st.session_state["auth"]
col_auth1, col_auth2 = st.columns([4, 1])
with col_auth1:
    if auth["role"] == "camp":
        st.caption(f":material/lock: **{auth['camp']}**로 로그인됨")
        my_camp = auth["camp"]
    else:
        st.caption(":material/lock: **관리자**로 로그인됨")
        admin_view_camp = st.selectbox(
            "캠프/창고로 보기 (알림 확인용)", ["선택 안 함"] + _login_accounts, key="admin_view_camp"
        )
        my_camp = admin_view_camp if admin_view_camp != "선택 안 함" else None
with col_auth2:
    if st.button("로그아웃", icon=":material/logout:"):
        del st.session_state["auth"]
        st.rerun()

if my_camp == "물류창고":
    try:
        pending_df = list_pending_warehouse_orders()
    except Exception:
        pending_df = pd.DataFrame()
    if not pending_df.empty:
        st.warning(
            f"**물류창고** 앞으로 승인 대기 중인 발주 요청이 **{len(pending_df)}건** 있어요. "
            "아래에서 바로 승인/거절할 수 있어요.",
            icon=":material/notifications_active:",
        )
        my_name_banner = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="banner_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name_banner
        for _, r in pending_df.iterrows():
            render_pending_warehouse_order_row(r, my_name_banner)
elif my_camp:
    try:
        pending_df = list_pending_transfer_requests_for_camp(my_camp)
    except Exception:
        pending_df = pd.DataFrame()
    if not pending_df.empty:
        st.warning(
            f"**{my_camp}** 앞으로 승인 대기 중인 이관 요청이 **{len(pending_df)}건** 있어요. "
            "아래에서 바로 승인/거절할 수 있어요.",
            icon=":material/notifications_active:",
        )
        my_name_banner = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="banner_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name_banner
        for _, r in pending_df.iterrows():
            render_pending_request_row(r, data, my_name_banner)

if auth["role"] == "admin":
    with st.expander("🔑 캠프/창고 비밀번호 관리 (관리자 전용)"):
        pw_camp = st.selectbox("캠프/창고 선택", _login_accounts, key="admin_pw_camp")
        new_pw = st.text_input("새 비밀번호", type="password", key="admin_pw_new")
        if st.button("비밀번호 설정", key="admin_pw_set_btn"):
            if new_pw.strip():
                set_camp_password(pw_camp, new_pw.strip())
                st.success(f"{pw_camp} 비밀번호를 설정했습니다.", icon=":material/check_circle:")
            else:
                st.error("비밀번호를 입력해주세요.", icon=":material/error:")

st.divider()


# ---------------- 파생 데이터 계산 ----------------

@st.cache_data(show_spinner=False)
def compute_camp_totals(data):
    totals = {c: {"qty": 0, "amt": 0} for c in data["campsOrder"]}
    for it in data["items"]:
        for camp, (q, a) in it["x"].items():
            if camp in totals:
                totals[camp]["qty"] += q
                totals[camp]["amt"] += a
    rows = []
    for c in data["campsOrder"]:
        rows.append({"캠프": c, "팀": data["campToTeam"].get(c, "-"), "재고 수량": totals[c]["qty"], "재고 금액": totals[c]["amt"]})
    return pd.DataFrame(rows)


camp_df = compute_camp_totals(data)
team_df = camp_df.groupby("팀", as_index=False)[["재고 수량", "재고 금액"]].sum().sort_values("재고 금액", ascending=False)
grand_qty = sum(it["q"] for it in data["items"])
grand_amt = sum(it["a"] for it in data["items"])
zero_camps = int((camp_df["재고 수량"] == 0).sum())

_cur_period_label = current_inventory_period()[2]
if st.session_state.get("_camp_value_snap_period") != _cur_period_label:
    try:
        save_inventory_value_snapshot("camp", build_camp_value_snapshot_rows(data))
        st.session_state["_camp_value_snap_period"] = _cur_period_label
    except Exception:
        pass  # 스냅샷 저장 실패해도 화면 표시는 계속 진행


def get_usage_for_code(code):
    if not usage or not code:
        return None
    return usage["items"].get(str(code).strip())


def weekly_usage_rate(code):
    """해당 SKU의 전체 캠프 합산 주 평균 사용량 (사용량 데이터 없으면 None)."""
    item_usage = get_usage_for_code(code)
    if not item_usage:
        return None
    total_avg = sum(u.get("avg", 0) for u in item_usage.values())
    return total_avg if total_avg > 0 else None


def estimate_depletion(qty, code):
    """qty(재고 수량)와 SKU의 주 평균 사용량으로 소진까지 남은 주 수와 예상 소진일을 계산."""
    rate = weekly_usage_rate(code)
    if not rate:
        return None, None
    weeks = qty / rate
    depletion_date = (datetime.now() + timedelta(weeks=weeks)).date()
    return round(weeks, 1), depletion_date


# ---------------- 탭 ----------------

(
    tab_overview, tab_camps, tab_items, tab_rebalance, tab_usage_amount,
    tab_forecast, tab_warehouse, tab_purchase, tab_total,
) = st.tabs(
    [
        ":material/dashboard: 개요",
        ":material/location_on: 캠프별 현황",
        ":material/search: 품목 검색",
        ":material/sync_alt: 재분배 도우미",
        ":material/payments: 월별 사용 금액",
        ":material/query_stats: 수요 예측",
        ":material/warehouse: 창고 현황",
        ":material/local_shipping: 발주 요청",
        ":material/inventory: 지바이크 전체 재고",
    ]
)

with tab_overview:
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("총 품목 수", f"{len(data['items']):,}종", border=True)
    k2.metric("총 재고 수량", f"{fmt_int(grand_qty)}개", border=True)
    k3.metric("총 재고 금액", fmt_won(grand_amt), border=True)
    k4.metric(
        "운영 캠프 수",
        f"{len(data['campsOrder'])}곳",
        delta=(f"재고 0인 캠프 {zero_camps}곳" if zero_camps else None),
        delta_color="inverse",
        border=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("RS팀별 재고 금액")
        st.bar_chart(team_df.set_index("팀")["재고 금액"])
    with c2:
        st.subheader("재고 금액 상위 캠프 TOP 8")
        top8 = camp_df.sort_values("재고 금액", ascending=False).head(8)
        st.bar_chart(top8.set_index("캠프")["재고 금액"])

with tab_camps:
    camp_weekly_amount = (usage.get("campWeeklyAmount") if usage else None) or {}
    camp_full_df = camp_df.copy()
    camp_full_df["주 사용 금액"] = camp_full_df["캠프"].map(camp_weekly_amount)
    camp_full_df["재고 보유(주)"] = camp_full_df.apply(
        lambda r: round(r["재고 금액"] / r["주 사용 금액"], 1) if r["주 사용 금액"] else None, axis=1
    )

    sort_col = st.selectbox(
        "정렬 기준", ["재고 금액", "재고 수량", "캠프", "팀", "재고 보유(주)"], index=0
    )
    ascending = st.checkbox("오름차순", value=False)
    camp_full_df = camp_full_df.sort_values(sort_col, ascending=ascending, na_position="last")

    st.caption(
        "재고 보유(주)는 캠프 사용량 엑셀 기준 주 평균 사용 금액 대비, 현재 재고 금액이 "
        "몇 주치인지를 나타내요. 사용량 데이터가 없으면 빈 칸으로 표시돼요."
    )

    money_df = camp_full_df.sort_values("재고 금액", ascending=False)
    camp_order = money_df["캠프"].tolist()
    coverage_df = camp_full_df.dropna(subset=["재고 보유(주)"])
    LINE_COLOR = "#F5A623"

    st.subheader("캠프별 재고 금액 · 재고 지수(보유 주수)")
    st.caption("막대 = 재고 금액(왼쪽 축), 선 = 재고 지수·재고 보유 주수(오른쪽 축, 주황색).")

    bars_chart = (
        alt.Chart(money_df)
        .mark_bar(color=ACCENT, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X(
                "캠프:N",
                sort=camp_order,
                title=None,
                axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=10, labelAngle=-60, domain=False, ticks=False),
            ),
            y=alt.Y(
                "재고 금액:Q",
                title="재고 금액",
                axis=alt.Axis(
                    titleColor=ACCENT, labelColor=ACCENT, labelFontSize=11, gridColor=CHART_GRID,
                    domain=False, ticks=False,
                ),
            ),
            tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
        )
    )

    line_chart = (
        alt.Chart(coverage_df)
        .mark_line(
            interpolate="monotone",
            color=LINE_COLOR,
            strokeWidth=2.5,
            point=alt.OverlayMarkDef(filled=True, fill=LINE_COLOR, stroke="#10141B", strokeWidth=1.5, size=45),
        )
        .encode(
            x=alt.X("캠프:N", sort=camp_order, title=None),
            y=alt.Y(
                "재고 보유(주):Q",
                title="재고 보유(주)",
                axis=alt.Axis(
                    titleColor=LINE_COLOR, labelColor=LINE_COLOR, labelFontSize=11, orient="right",
                    domain=False, ticks=False, grid=False,
                ),
            ),
            tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("재고 보유(주):Q", format=",.1f")],
        )
    )

    combo_chart = (
        alt.layer(bars_chart, line_chart)
        .resolve_scale(y="independent")
        .properties(height=360)
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(combo_chart, width="stretch")

    st.dataframe(
        camp_full_df,
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%,d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
            "주 사용 금액": st.column_config.NumberColumn(format="₩%,d"),
            "재고 보유(주)": st.column_config.NumberColumn(format="%.1f주"),
        },
    )

with tab_items:
    search = st.text_input("부품명 또는 부품 번호로 검색", "")
    items = data["items"]
    if search.strip():
        q = search.strip().lower()
        items = [it for it in items if q in it["n"].lower() or q in str(it["c"]).lower()]
    st.caption(f"{len(items):,}개 품목 중 최대 50개 표시")
    monthly_item_camp_qty = (usage.get("monthlyItemCampQty") if usage else None) or {}
    for it in items[:50]:
        camp_usage = get_usage_for_code(it["c"])
        with st.expander(f"{it['n']}  ·  {it['c']}  ·  총 {fmt_int(it['q'])}개  ·  {fmt_won(it['a'])}"):
            if not it["x"]:
                st.caption("보유 캠프 없음")
            else:
                rows = []
                for camp, (q, a) in sorted(it["x"].items(), key=lambda kv: -kv[1][0]):
                    avg = camp_usage.get(camp, {}).get("avg") if camp_usage else None
                    rows.append({"캠프": camp, "재고 수량": q, "주 평균 사용량": avg if avg is not None else "-"})
                st.dataframe(pd.DataFrame(rows), hide_index=True)

            if not camp_usage:
                st.caption("이 품목의 사용량 데이터가 없어요.")
            else:
                st.markdown("**사용량 추이**")
                gran_col, scope_col = st.columns(2)
                granularity = gran_col.radio(
                    "기간 단위", ["주별", "월별"], horizontal=True, key=f"usage_gran_{it['c']}"
                )
                usage_camps = sorted(camp_usage.keys())
                scope = scope_col.selectbox(
                    "범위", ["지바이크 전체"] + usage_camps, key=f"usage_scope_{it['c']}"
                )

                if granularity == "주별":
                    if scope == "지바이크 전체":
                        weekly_totals = {}
                        for camp, u in camp_usage.items():
                            for y, w, qv in u["w"]:
                                weekly_totals[(y, w)] = weekly_totals.get((y, w), 0) + qv
                        series = sorted(weekly_totals.items())
                    else:
                        u = camp_usage.get(scope)
                        series = [((y, w), qv) for y, w, qv in u["w"]] if u else []
                    trend_df = pd.DataFrame(
                        {"주차": [f"{y}-{w}" for (y, w), _ in series], "사용량": [v for _, v in series]}
                    )
                    x_col = "주차"
                else:
                    item_monthly = monthly_item_camp_qty.get(it["c"], {})
                    months = sorted(item_monthly.keys())
                    if scope == "지바이크 전체":
                        values = [sum(item_monthly[m].values()) for m in months]
                    else:
                        values = [item_monthly[m].get(scope, 0) for m in months]
                    trend_df = pd.DataFrame({"월": months, "사용량": values})
                    x_col = "월"

                if trend_df.empty:
                    st.caption("표시할 데이터가 없어요.")
                else:
                    st.altair_chart(render_trend_chart(trend_df, x_col, "사용량"), width="stretch")
                    wide_df = trend_df.set_index(x_col).T
                    st.dataframe(wide_df, hide_index=False)

with tab_rebalance:
    st.caption("품목을 선택하면 캠프별 재고 편차를 확인할 수 있어요")
    query = st.text_input("재분배를 검토할 품목명 또는 번호 입력", "", key="rebalance_query")
    selected = None
    if query.strip():
        q = query.strip().lower()
        candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()][:8]
        if candidates:
            options = {f"{it['n']} ({it['c']})": it for it in candidates}
            picked_label = st.radio("검색 결과", list(options.keys()), index=0)
            selected = options[picked_label]
        else:
            st.info("일치하는 품목이 없습니다.", icon=":material/search_off:")

    if selected:
        item_usage = get_usage_for_code(selected["c"])
        req_df = list_transfer_requests(selected["c"])
        if not req_df.empty:
            # 승인(이동중) 상태는 아직 실제 재고에서 빠지지 않았지만(입고완료 시에만 차감),
            # 이미 다른 곳으로 나가기로 확정된 수량이라 "가용재고" 계산에서는 미리 빼준다.
            in_transit_out = req_df[req_df["status"] == "in_transit"].groupby("from_camp")["qty"].sum()
        else:
            in_transit_out = pd.Series(dtype=int)

        rows = []
        for camp in data["campsOrder"]:
            pair = selected["x"].get(camp)
            qty = pair[0] if pair else 0
            intransit_qty = int(in_transit_out.get(camp, 0))
            u = item_usage.get(camp) if item_usage else None
            avg = u["avg"] if u else None
            weeks_of_stock = round(qty / avg, 1) if avg and avg > 0 else None
            rows.append(
                {
                    "팀": data["campToTeam"].get(camp, "-"),
                    "캠프": camp,
                    "재고 수량": qty,
                    "가용재고": qty - intransit_qty,  # 재고 수량 - 이동중(승인됐지만 아직 미입고)
                    "이동중재고": intransit_qty,  # 승인되어 이 캠프에서 나가는 중인 수량 (입고완료 전)
                    "주 평균 사용량": avg,  # None(NaN) 또는 숫자
                    "소진 예상(주)": weeks_of_stock,  # None(NaN) 또는 숫자
                }
            )
        df_rows = pd.DataFrame(rows).sort_values("재고 수량", ascending=False)

        # 재분배 제안 (NaN은 항상 False로 비교되므로 안전)
        suggested_transfer = None
        shortages = df_rows[(df_rows["재고 수량"] == 0) & (df_rows["주 평균 사용량"] > 0)]
        if not shortages.empty:
            donors = df_rows[(df_rows["재고 수량"] > 1)]
            donors = donors[donors["소진 예상(주)"].isna() | (donors["소진 예상(주)"] > 4)]
            donors = donors.sort_values("재고 수량", ascending=False)
            if not donors.empty:
                top = donors.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(shortages["캠프"].head(3).tolist())
                suggested_transfer = {"from": top["캠프"], "to": shortages["캠프"].iloc[0], "qty": move_qty}
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (최근 사용량 데이터를 반영한 제안)",
                    icon=":material/lightbulb:",
                )
        else:
            with_stock = df_rows[df_rows["재고 수량"] > 0]
            empty = df_rows[df_rows["재고 수량"] == 0]
            if not with_stock.empty and not empty.empty and with_stock.iloc[0]["재고 수량"] >= 2:
                top = with_stock.iloc[0]
                move_qty = max(1, int(top["재고 수량"] // 2))
                to_camps = ", ".join(empty["캠프"].head(3).tolist())
                suggested_transfer = {"from": top["캠프"], "to": empty["캠프"].iloc[0], "qty": move_qty}
                st.warning(
                    f"**{top['캠프']}**에 {fmt_int(top['재고 수량'])}개 보유 중인 반면, **{to_camps}**에는 재고가 없습니다. "
                    f"약 {move_qty}개 이동을 검토해보세요. (사용량 데이터가 없어 재고량만 기준으로 한 참고용 제안)",
                    icon=":material/lightbulb:",
                )

        st.subheader("캠프 간 재고 이관")
        st.caption("요청 → 승인(이동중) → 입고완료 순서로 처리돼요. 실제 재고 수량은 입고완료 시점에 보내는 캠프에서 빠지고 받는 캠프에 더해지며, 승인되면 그 전까지는 \"가용재고\"에서만 미리 제외되어 보입니다.")

        my_name = st.text_input(
            "내 이름", value=st.session_state.get("transfer_my_name", ""), key="transfer_my_name_input"
        )
        st.session_state["transfer_my_name"] = my_name

        item_code = selected["c"]
        with st.form(f"transfer_request_form_{item_code}"):
            camps_order = data["campsOrder"]
            default_from = camps_order.index(suggested_transfer["from"]) if suggested_transfer else 0
            default_to = camps_order.index(suggested_transfer["to"]) if suggested_transfer else min(1, len(camps_order) - 1)
            fc1, fc2, fc3 = st.columns([2, 2, 1])
            with fc1:
                from_camp_sel = st.selectbox("보내는 캠프", camps_order, index=default_from)
            with fc2:
                to_camp_sel = st.selectbox("받는 캠프", camps_order, index=default_to)
            with fc3:
                qty_sel = st.number_input(
                    "수량", min_value=1, step=1, value=suggested_transfer["qty"] if suggested_transfer else 1
                )
            if st.form_submit_button("이관 요청"):
                if not my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                elif from_camp_sel == to_camp_sel:
                    st.error("보내는 캠프와 받는 캠프가 같습니다.", icon=":material/error:")
                else:
                    create_transfer_request(
                        item_code, selected["n"], from_camp_sel, to_camp_sel, int(qty_sel), my_name.strip()
                    )
                    st.success("이관 요청을 등록했습니다.", icon=":material/send:")
                    st.rerun()

        req_df = list_transfer_requests(item_code)
        requested_rows = req_df[req_df["status"] == "requested"] if not req_df.empty else req_df
        in_transit_rows = req_df[req_df["status"] == "in_transit"] if not req_df.empty else req_df
        done_rows = req_df[req_df["status"].isin(["completed", "rejected"])] if not req_df.empty else req_df

        if not requested_rows.empty:
            st.markdown("**요청중**")
            for _, r in requested_rows.iterrows():
                render_pending_request_row(r, data, my_name)

        if not in_transit_rows.empty:
            st.markdown("**이동중**")
            for _, r in in_transit_rows.iterrows():
                c0, c1, c2 = st.columns([1, 5, 1])
                c0.badge("이동중", icon=":material/local_shipping:", color="blue")
                c1.write(
                    f"{r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · 승인자: {r['approved_by']}"
                )
                if c2.button("입고완료", key=f"receive_{r['id']}"):
                    if not my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        item = find_item_by_code(data, item_code)
                        try:
                            moved_amt = deduct_camp_stock(item, r["from_camp"], int(r["qty"]))
                        except ValueError as e:
                            st.error(str(e), icon=":material/error:")
                        else:
                            add_camp_stock(item, r["to_camp"], int(r["qty"]), moved_amt)
                            save_data("inventory", data)
                            complete_transfer_request(r["id"], my_name.strip(), moved_amt)
                            st.success("입고 완료 처리했습니다.", icon=":material/inventory_2:")
                            st.rerun()

        if not done_rows.empty:
            with st.expander(f"완료/거절 내역 ({len(done_rows)}건)", icon=":material/history:"):
                for _, r in done_rows.iterrows():
                    is_done = r["status"] == "completed"
                    who = r["received_by"] if is_done else r["approved_by"]
                    dc0, dc1 = st.columns([1, 5])
                    if is_done:
                        dc0.badge("입고완료", icon=":material/check_circle:", color="green")
                    else:
                        dc0.badge("거절", icon=":material/cancel:", color="red")
                    dc1.caption(f"{r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · {who}")

        if item_usage:
            weekly_totals = {}
            for camp, u in item_usage.items():
                for y, w, qv in u["w"]:
                    key = (y, w)
                    weekly_totals[key] = weekly_totals.get(key, 0) + qv
            trend = sorted(weekly_totals.items())[-12:]
            if trend:
                trend_df = pd.DataFrame(
                    {"주차": [f"{y}-{w}" for (y, w), _ in trend], "사용량": [v for _, v in trend]}
                )
                st.subheader("전체 캠프 합산 · 최근 12주 사용량 추이")
                st.altair_chart(render_trend_chart(trend_df, "주차", "사용량"), width="stretch")
        else:
            st.caption("이 품목의 사용량 데이터가 아직 없습니다. (재고 수량만으로 비교합니다)")

        st.dataframe(
            df_rows,
            hide_index=True,
            column_config={
                "재고 수량": st.column_config.NumberColumn(format="%,d개"),
                "가용재고": st.column_config.NumberColumn(format="%,d개"),
                "이동중재고": st.column_config.NumberColumn(format="%,d개"),
                "주 평균 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
                "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
            },
        )

with tab_usage_amount:
    monthly_camp_amount = usage.get("monthlyCampAmount") if usage else None
    if not monthly_camp_amount:
        st.info("사용량 엑셀을 업로드하면 캠프별·월별 사용 금액을 확인할 수 있어요. (JSON 업로드에는 이 데이터가 없어요)", icon=":material/payments:")
    else:
        months = sorted(monthly_camp_amount.keys())
        amt_rows = [
            {"월": m, "캠프": camp, "금액": amt}
            for m in months
            for camp, amt in monthly_camp_amount[m].items()
        ]
        amt_df = pd.DataFrame(amt_rows)

        monthly_total = amt_df.groupby("월")["금액"].sum().reindex(months, fill_value=0)
        grand_total = monthly_total.sum()
        monthly_avg = monthly_mean_excluding_current(monthly_total)

        k1, k2, k3 = st.columns(3)
        k1.metric(
            "총 사용 금액",
            fmt_won(grand_total),
            border=True,
            chart_data=monthly_total,
            chart_type="area",
        )
        k2.metric("월평균 사용 금액", fmt_won(monthly_avg), border=True)
        k3.metric("데이터 기간", f"{months[0]} ~ {months[-1]} ({len(months)}개월)", border=True)
        st.caption("이번 달은 아직 마감 전이라 월평균 계산에서는 제외했어요 (그래프·합계에는 포함돼요).")

        st.subheader("전체 캠프 합산 · 월별 사용 금액 추이")
        overall_trend_df = monthly_total.reset_index()
        overall_trend_df.columns = ["월", "금액"]
        st.altair_chart(render_trend_chart(overall_trend_df, "월", "금액"), width="stretch")
        st.dataframe(
            overall_trend_df.sort_values("월", ascending=False),
            hide_index=True,
            column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
        )

        sub_camp, sub_sku = st.tabs([":material/location_on: 캠프별 사용 금액", ":material/settings: 부품별 사용 금액"])

        with sub_camp:
            camp_totals = (
                amt_df.groupby("캠프")["금액"].sum().sort_values(ascending=False).reset_index()
            )
            camp_totals.columns = ["캠프", "총 사용 금액"]

            picked_camp = st.selectbox("캠프 선택", ["전체 캠프 합산"] + camp_totals["캠프"].tolist())
            if picked_camp != "전체 캠프 합산":
                camp_series = (
                    amt_df[amt_df["캠프"] == picked_camp].set_index("월")["금액"].reindex(months, fill_value=0)
                )
                cc1, cc2 = st.columns(2)
                cc1.metric(f"{picked_camp} 총 사용 금액", fmt_won(camp_series.sum()), border=True)
                cc2.metric(
                    f"{picked_camp} 월평균 사용 금액",
                    fmt_won(monthly_mean_excluding_current(camp_series)),
                    border=True,
                )
                camp_trend_df = camp_series.reset_index()
                camp_trend_df.columns = ["월", "금액"]
                st.altair_chart(render_trend_chart(camp_trend_df, "월", "금액"), width="stretch")
                st.dataframe(
                    camp_trend_df.sort_values("월", ascending=False),
                    hide_index=True,
                    column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
                )

            st.subheader("캠프별 총 사용 금액 순위 (전체 기간 합계)")
            camp_bar = (
                alt.Chart(camp_totals)
                .mark_bar(color=ACCENT, cornerRadiusTopRight=3, cornerRadiusBottomRight=3, height=14)
                .encode(
                    y=alt.Y(
                        "캠프:N",
                        sort="-x",
                        title=None,
                        axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False),
                    ),
                    x=alt.X(
                        "총 사용 금액:Q",
                        title=None,
                        axis=alt.Axis(
                            labelColor=CHART_MUTED, labelFontSize=11, gridColor=CHART_GRID, domain=False, ticks=False
                        ),
                    ),
                    tooltip=[alt.Tooltip("캠프:N"), alt.Tooltip("총 사용 금액:Q", format=",.0f")],
                )
                .properties(height=max(220, len(camp_totals) * 20))
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(camp_bar, width="stretch")
            st.dataframe(
                camp_totals,
                hide_index=True,
                column_config={"총 사용 금액": st.column_config.NumberColumn(format="₩%,d")},
            )

        with sub_sku:
            monthly_sku_amount = usage.get("monthlySkuAmount")
            sku_names = usage.get("skuNames") or {}
            if not monthly_sku_amount:
                st.caption("이 사용량 데이터에는 부품별 금액 정보가 없어요. 사용량 엑셀을 다시 업로드하면 채워집니다.")
            else:
                sku_amt_rows = [
                    {"SKU": sku, "월": m, "금액": amt}
                    for m in months
                    for sku, amt in monthly_sku_amount.get(m, {}).items()
                ]
                sku_amt_df = pd.DataFrame(sku_amt_rows)

                sku_totals = sku_amt_df.groupby("SKU")["금액"].sum().sort_values(ascending=False).reset_index()
                sku_totals.columns = ["SKU", "총 사용 금액"]
                sku_totals["품명"] = sku_totals["SKU"].map(sku_names).fillna("-")

                sku_weekly_amount = usage.get("skuWeeklyAmount") or {}
                sku_totals["주평균 사용금액"] = sku_totals["SKU"].map(sku_weekly_amount)

                sku_monthly_pivot = (
                    sku_amt_df.pivot_table(index="SKU", columns="월", values="금액", fill_value=0)
                    .reindex(columns=months, fill_value=0)
                )
                sku_monthly_avg = sku_monthly_pivot.apply(monthly_mean_excluding_current, axis=1)
                sku_totals["월평균 사용금액"] = sku_totals["SKU"].map(sku_monthly_avg)

                # 품명이 같은 부품이 섞여 있을 수 있어, 그래프/표 표시용으로는 SKU를 덧붙여 구분한다.
                sku_totals["표시명"] = sku_totals["품명"] + " (" + sku_totals["SKU"] + ")"
                sku_totals = sku_totals[
                    ["표시명", "품명", "SKU", "총 사용 금액", "주평균 사용금액", "월평균 사용금액"]
                ]

                sku_query = st.text_input("SKU 또는 품명으로 검색해서 월별 추이 보기", "", key="usage_sku_query")
                if sku_query.strip():
                    q = sku_query.strip().lower()
                    candidates = [
                        s for s in sku_totals["SKU"] if q in s.lower() or q in str(sku_names.get(s, "")).lower()
                    ][:8]
                    if candidates:
                        labels = {f"{s} · {sku_names.get(s, '-')}": s for s in candidates}
                        picked_label = st.radio("검색 결과", list(labels.keys()), key="usage_sku_radio")
                        picked_sku = labels[picked_label]
                        sku_series = (
                            sku_amt_df[sku_amt_df["SKU"] == picked_sku]
                            .set_index("월")["금액"]
                            .reindex(months, fill_value=0)
                        )
                        sc1, sc2 = st.columns(2)
                        sc1.metric(f"{picked_label} 총 사용 금액", fmt_won(sku_series.sum()), border=True)
                        sc2.metric(
                            f"{picked_label} 월평균 사용 금액",
                            fmt_won(monthly_mean_excluding_current(sku_series)),
                            border=True,
                        )
                        sku_trend_df = sku_series.reset_index()
                        sku_trend_df.columns = ["월", "금액"]
                        st.altair_chart(render_trend_chart(sku_trend_df, "월", "금액"), width="stretch")
                        st.dataframe(
                            sku_trend_df.sort_values("월", ascending=False),
                            hide_index=True,
                            column_config={"금액": st.column_config.NumberColumn(format="₩%,d")},
                        )
                    else:
                        st.caption("일치하는 부품이 없습니다.")

                st.subheader("부품별 총 사용 금액 순위 (전체 기간 합계)")
                top_n = 25
                sku_bar_df = sku_totals.head(top_n)
                sku_bar = (
                    alt.Chart(sku_bar_df)
                    .mark_bar(color=ACCENT, cornerRadiusTopRight=3, cornerRadiusBottomRight=3, height=14)
                    .encode(
                        y=alt.Y(
                            "표시명:N",
                            sort="-x",
                            title=None,
                            axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False),
                        ),
                        x=alt.X(
                            "총 사용 금액:Q",
                            title=None,
                            axis=alt.Axis(
                                labelColor=CHART_MUTED,
                                labelFontSize=11,
                                gridColor=CHART_GRID,
                                domain=False,
                                ticks=False,
                            ),
                        ),
                        tooltip=[
                            alt.Tooltip("품명:N"),
                            alt.Tooltip("SKU:N"),
                            alt.Tooltip("총 사용 금액:Q", format=",.0f"),
                        ],
                    )
                    .properties(height=max(220, len(sku_bar_df) * 20))
                    .configure_view(strokeWidth=0)
                    .configure(background="transparent")
                )
                st.altair_chart(sku_bar, width="stretch")
                st.caption(f"상위 {top_n}개만 그래프로 표시했어요 (전체 {len(sku_totals):,}개 부품은 아래 표에서 검색 가능).")

                sku_table_search = st.text_input("SKU 또는 품명으로 검색 (표)", "", key="usage_sku_table_search")
                sku_table_df = sku_totals
                if sku_table_search.strip():
                    q = sku_table_search.strip().lower()
                    sku_table_df = sku_table_df[
                        sku_table_df["SKU"].str.lower().str.contains(q, regex=False)
                        | sku_table_df["품명"].str.lower().str.contains(q, regex=False)
                    ]
                st.dataframe(
                    sku_table_df[["품명", "SKU", "총 사용 금액", "주평균 사용금액", "월평균 사용금액"]],
                    hide_index=True,
                    column_config={
                        "총 사용 금액": st.column_config.NumberColumn(format="₩%,d"),
                        "주평균 사용금액": st.column_config.NumberColumn(format="₩%,d"),
                        "월평균 사용금액": st.column_config.NumberColumn(format="₩%,d"),
                    },
                )

with tab_forecast:
    st.caption(
        "캠프별로 품목별 다음 몇 달치 예상 사용량을 직접 입력해요. 최근 3개월(이번 달 제외) 평균이 "
        "참고용으로 먼저 채워지고, 필요하면 조정해서 저장하면 됩니다. "
        "(추후 회귀분석 기반 자동 예측으로 보완/대체할 예정)"
    )

    fc_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="fc_my_name_input"
    )
    st.session_state["transfer_my_name"] = fc_my_name

    fc_camp = st.selectbox("캠프 선택", data["campsOrder"], key="fc_camp_select")

    fc_now = datetime.now()
    fc_month_options = []
    fy, fm = fc_now.year, fc_now.month
    for _ in range(3):
        fm += 1
        if fm > 12:
            fm = 1
            fy += 1
        fc_month_options.append((fy, fm))
    fc_month_labels = [f"{y}-{m:02d}" for y, m in fc_month_options]
    fc_month_label = st.selectbox("예측 대상 월", fc_month_labels, key="fc_month_select")
    fc_year, fc_month = fc_month_options[fc_month_labels.index(fc_month_label)]

    try:
        with st.spinner("최근 사용 품목을 불러오는 중이에요..."):
            top_items_df = top_camp_items_recent(fc_camp, limit=30)
            existing_df = load_demand_forecasts(fc_camp, fc_year, fc_month)
    except Exception as e:
        st.error(f"데이터를 불러오는 중 문제가 발생했습니다: {e}", icon=":material/error:")
    else:
        if top_items_df.empty:
            st.caption(f"{fc_camp}의 최근 3개월 사용량 데이터가 없어요.")
        else:
            existing_by_code = (
                {row["item_code"]: row for _, row in existing_df.iterrows()} if not existing_df.empty else {}
            )

            fc_rows = []
            for _, r in top_items_df.iterrows():
                code = r["item_code"]
                existing = existing_by_code.get(code)
                default_qty = (
                    float(existing["predicted_qty"]) if existing is not None else round(r["avg_qty"] or 0, 1)
                )
                fc_rows.append(
                    {
                        "SKU": code,
                        "품명": r["item_name"] or "-",
                        "최근 3개월 평균": r["avg_qty"],
                        "예측 수량": default_qty,
                    }
                )
            fc_table_df = pd.DataFrame(fc_rows)

            st.caption(f"{fc_camp} · {fc_month_label} 예측 (최근 사용량 상위 {len(fc_table_df)}개 품목)")
            with st.form(f"fc_form_{fc_camp}_{fc_year}_{fc_month}"):
                fc_edited_df = st.data_editor(
                    fc_table_df,
                    hide_index=True,
                    disabled=["SKU", "품명", "최근 3개월 평균"],
                    column_config={
                        "최근 3개월 평균": st.column_config.NumberColumn(format="%.1f개"),
                        "예측 수량": st.column_config.NumberColumn(format="%d개", min_value=0, step=1),
                    },
                    key=f"fc_editor_{fc_camp}_{fc_year}_{fc_month}",
                )
                fc_submitted = st.form_submit_button("예측 저장", type="primary", icon=":material/save:")

            if fc_submitted:
                if not fc_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    save_rows = [
                        {
                            "camp": fc_camp,
                            "item_code": r["SKU"],
                            "item_name": r["품명"],
                            "forecast_year": fc_year,
                            "forecast_month": fc_month,
                            "predicted_qty": float(r["예측 수량"]),
                            "historical_avg_qty": (
                                float(r["최근 3개월 평균"]) if pd.notna(r["최근 3개월 평균"]) else None
                            ),
                            "entered_by": fc_my_name.strip(),
                        }
                        for _, r in fc_edited_df.iterrows()
                    ]
                    save_demand_forecasts(save_rows)
                    st.success(
                        f"{fc_camp} {fc_month_label} 예측 {len(save_rows)}건을 저장했습니다.",
                        icon=":material/check_circle:",
                    )
                    st.rerun()

    st.subheader("저장된 예측 이력")
    fc_hist_df = list_demand_forecasts(camp=fc_camp)
    if fc_hist_df.empty:
        st.caption("아직 저장된 예측이 없어요.")
    else:
        fc_hist_display = fc_hist_df.copy()
        fc_hist_display["예측 대상월"] = (
            fc_hist_display["forecast_year"].astype(str) + "-"
            + fc_hist_display["forecast_month"].astype(str).str.zfill(2)
        )
        st.dataframe(
            fc_hist_display[
                ["예측 대상월", "item_name", "item_code", "predicted_qty", "historical_avg_qty", "entered_by"]
            ].rename(columns={"item_name": "품명", "item_code": "SKU"}),
            hide_index=True,
            column_config={
                "predicted_qty": st.column_config.NumberColumn("예측 수량", format="%.1f개"),
                "historical_avg_qty": st.column_config.NumberColumn("최근 3개월 평균", format="%.1f개"),
            },
        )

with tab_total:
    if not get_boxhero_token():
        st.info(
            "박스히어로 연동이 설정되면 창고 + 캠프 전체 재고를 함께 볼 수 있어요.",
            icon=":material/link_off:",
        )
    else:
        try:
            with st.spinner("박스히어로에서 창고 데이터를 가져오는 중이에요..."):
                wh_items = fetch_boxhero_items()
        except Exception as e:
            st.error(f"박스히어로 연동 중 문제가 발생했습니다: {e}", icon=":material/error:")
        else:
            warehouse_qty = sum(it.get("quantity", 0) for it in wh_items)
            warehouse_amt = sum(it.get("quantity", 0) * float(it.get("price") or 0) for it in wh_items)
            camp_qty = grand_qty
            camp_amt = grand_amt
            total_qty = warehouse_qty + camp_qty
            total_amt = warehouse_amt + camp_amt

            st.caption("수량 기준")
            k1, k2, k3 = st.columns(3)
            k1.metric("지바이크 전체 재고 수량", f"{fmt_int(total_qty)}개", border=True)
            k2.metric("캠프 재고 (36곳 합계)", f"{fmt_int(camp_qty)}개", border=True)
            k3.metric("물류창고 재고", f"{fmt_int(warehouse_qty)}개", border=True)

            st.caption("금액 기준")
            m1, m2, m3 = st.columns(3)
            m1.metric("지바이크 전체 재고 금액", fmt_won(total_amt), border=True)
            m2.metric("캠프 재고 금액", fmt_won(camp_amt), border=True)
            m3.metric("물류창고 재고 금액", fmt_won(warehouse_amt), border=True)
            st.caption(
                "창고 재고 금액은 원가(cost) 입력이 대부분 비어있어 판매가(price) 기준으로 계산했어요 "
                "— 캠프 재고 금액과 산정 기준이 달라 완전히 동일한 비교는 아니에요."
            )

            breakdown_df = pd.DataFrame(
                {"구분": ["캠프 (36곳 합계)", "물류창고"], "재고 금액": [camp_amt, warehouse_amt]}
            )
            st.subheader("창고 vs 캠프 재고 금액 비중")
            breakdown_bar = (
                alt.Chart(breakdown_df)
                .mark_bar(color=ACCENT, cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=32)
                .encode(
                    y=alt.Y(
                        "구분:N",
                        sort="-x",
                        title=None,
                        axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=12, domain=False, ticks=False),
                    ),
                    x=alt.X(
                        "재고 금액:Q",
                        title=None,
                        axis=alt.Axis(
                            labelColor=CHART_MUTED, labelFontSize=11, gridColor=CHART_GRID, domain=False, ticks=False
                        ),
                    ),
                    tooltip=[alt.Tooltip("구분:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
                )
                .properties(height=140)
                .configure_view(strokeWidth=0)
                .configure(background="transparent")
            )
            st.altair_chart(breakdown_bar, width="stretch")

            st.subheader("주별 재고 금액 추이 (창고 + 캠프)")
            trend_raw = load_inventory_value_trend()
            if trend_raw.empty:
                st.caption("아직 쌓인 현황이 없어요. 이번 주부터 자동으로 쌓여요 (매주 처음 접속할 때 그 주의 최신 값으로 갱신돼요).")
            else:
                trend_wide = trend_raw.pivot_table(
                    index="period_label", columns="source", values="amt", aggfunc="sum", fill_value=0
                ).reset_index()
                for col in ("camp", "warehouse"):
                    if col not in trend_wide.columns:
                        trend_wide[col] = 0
                trend_wide["전체"] = trend_wide["camp"] + trend_wide["warehouse"]
                trend_wide = trend_wide.rename(columns={"camp": "캠프", "warehouse": "물류창고"})
                trend_long = trend_wide.melt(
                    id_vars="period_label", value_vars=["전체", "캠프", "물류창고"], var_name="구분", value_name="재고 금액"
                )
                trend_line = (
                    alt.Chart(trend_long)
                    .mark_line(point=True, strokeWidth=2)
                    .encode(
                        x=alt.X(
                            "period_label:O",
                            sort=None,
                            title=None,
                            axis=alt.Axis(labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False, grid=False),
                        ),
                        y=alt.Y(
                            "재고 금액:Q",
                            title=None,
                            axis=alt.Axis(
                                labelColor=CHART_MUTED, labelFontSize=11, domain=False, ticks=False,
                                gridColor=CHART_GRID, tickCount=4,
                            ),
                        ),
                        color=alt.Color("구분:N", scale=alt.Scale(scheme="category10"), legend=alt.Legend(title=None, labelColor=CHART_MUTED)),
                        tooltip=[alt.Tooltip("period_label:O", title="주차"), alt.Tooltip("구분:N"), alt.Tooltip("재고 금액:Q", format=",.0f")],
                    )
                    .properties(height=240)
                    .configure_view(strokeWidth=0)
                    .configure(background="transparent")
                )
                st.altair_chart(trend_line, width="stretch")
                st.caption(
                    "스냅샷 시점 단가 기준 금액이에요. SKU별 수량은 그대로 쌓이고 있어서, 나중에 필요하면 "
                    "같은 기준(예: 현재 단가)으로 다시 계산한 추이도 만들 수 있어요."
                )

            st.subheader("SKU별 창고-캠프 재고 비교")
            camp_by_sku = {
                it["c"].strip().upper(): it for it in data["items"] if it.get("c")
            }
            wh_by_sku = {it["sku"].strip().upper(): it for it in wh_items if it.get("sku")}
            all_skus = set(camp_by_sku) | set(wh_by_sku)

            compare_rows = []
            for sku in all_skus:
                camp_it = camp_by_sku.get(sku)
                wh_it = wh_by_sku.get(sku)
                camp_q = camp_it["q"] if camp_it else 0
                camp_a = camp_it["a"] if camp_it else 0
                wh_q = wh_it.get("quantity", 0) if wh_it else 0
                wh_price = float(wh_it.get("price") or 0) if wh_it else 0
                wh_a = wh_q * wh_price
                name = camp_it["n"] if camp_it else wh_it["name"]
                total_q = wh_q + camp_q
                # 사용량 조회는 원래 표기(대소문자)를 써야 정확히 매칭된다 (join용 sku는 대문자로 통일돼있음).
                orig_code = camp_it["c"] if camp_it else wh_it["sku"]
                weeks, depletion_date = estimate_depletion(total_q, orig_code)
                compare_rows.append(
                    {
                        "SKU": sku,
                        "품명": name,
                        "물류창고 수량": wh_q,
                        "캠프 재고 수량": camp_q,
                        "합계 수량": total_q,
                        "재고 금액": wh_a + camp_a,
                        "주 사용량": weekly_usage_rate(orig_code),
                        "소진 예상(주)": weeks,
                        "예상 소진일": depletion_date.isoformat() if depletion_date else None,
                    }
                )
            compare_df = pd.DataFrame(compare_rows).sort_values("재고 금액", ascending=False)

            compare_search = st.text_input("SKU 또는 품명으로 검색", "", key="total_compare_search")
            if compare_search.strip():
                q = compare_search.strip().lower()
                compare_df = compare_df[
                    compare_df["SKU"].str.lower().str.contains(q, regex=False)
                    | compare_df["품명"].str.lower().str.contains(q, regex=False)
                ]
            st.caption(
                f"{len(compare_df):,}개 SKU (창고·캠프 어느 한쪽에라도 있는 품목 전체). "
                "주 사용량/예상 소진일은 캠프 사용량 엑셀 기준(전체 캠프 합산)이에요."
            )
            display_compare_df = compare_df.copy()
            display_compare_df["예상 소진일"] = display_compare_df["예상 소진일"].fillna("-")
            st.dataframe(
                display_compare_df,
                hide_index=True,
                column_config={
                    "물류창고 수량": st.column_config.NumberColumn(format="%,d개"),
                    "캠프 재고 수량": st.column_config.NumberColumn(format="%,d개"),
                    "합계 수량": st.column_config.NumberColumn(format="%,d개"),
                    "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
                    "주 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
                    "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
                },
            )

with tab_purchase:
    st.caption(
        "품목을 선택하고 받을 캠프를 지정해서 물류창고에 발주를 요청할 수 있어요. "
        "승인은 \"창고에서 발송 준비\" 단계이며, 박스히어로 실제 재고는 여기서 자동으로 바뀌지 않아요 "
        "(창고 쪽은 수동으로 처리해주세요). 입고완료를 눌러야 캠프 재고에 반영됩니다."
    )

    po_my_name = st.text_input(
        "내 이름", value=st.session_state.get("transfer_my_name", ""), key="po_my_name_input"
    )
    st.session_state["transfer_my_name"] = po_my_name

    po_camp = st.selectbox("받는 캠프", data["campsOrder"], key="po_camp_select")
    st.caption(f"아래에서 담는 품목은 모두 **{po_camp}** 앞으로 발주돼요. 캠프를 바꾸면 그 다음부터 담는 품목에 적용돼요.")

    if "po_cart" not in st.session_state:
        st.session_state["po_cart"] = []

    st.subheader("발주 목록에 담기")
    po_query = st.text_input("발주할 품목명 또는 번호 입력", "", key="po_query")
    po_selected = None
    if po_query.strip():
        q = po_query.strip().lower()
        po_candidates = [it for it in data["items"] if q in it["n"].lower() or q in str(it["c"]).lower()][:8]
        if po_candidates:
            po_options = {f"{it['n']} ({it['c']})": it for it in po_candidates}
            po_picked_label = st.radio("검색 결과", list(po_options.keys()), key="po_radio")
            po_selected = po_options[po_picked_label]
        else:
            st.info("일치하는 품목이 없습니다.", icon=":material/search_off:")

    if po_selected:
        po_item_code = po_selected["c"]
        po_item_name = po_selected["n"]
        with st.form(f"po_form_{po_item_code}_{po_camp}"):
            st.caption(f"받는 캠프: **{po_camp}**")
            po_camp_avg = camp_weekly_usage_rate(po_item_code, po_camp)
            if po_camp_avg:
                st.caption(f"{po_camp}의 이 품목 주 평균 사용량: {po_camp_avg:g}개/주")
            else:
                st.caption(f"{po_camp}의 이 품목 사용량 데이터가 없어요 (2배 초과 검증을 생략해요).")
            po_qty = st.number_input("발주 수량", min_value=1, step=1, value=1, key=f"po_qty_{po_item_code}")
            po_threshold = po_camp_avg * 2 if po_camp_avg else None
            po_over = po_threshold is not None and po_qty > po_threshold
            po_reason = ""
            if po_over:
                st.warning(
                    f"주 평균 사용량({po_camp_avg:g}개)의 2배({po_threshold:g}개)를 초과하는 발주예요. "
                    "사유를 입력해주세요.",
                    icon=":material/warning:",
                )
                po_reason = st.text_area("발주 사유", key=f"po_reason_{po_item_code}")
            if st.form_submit_button("목록에 추가", icon=":material/add:"):
                if po_over and not po_reason.strip():
                    st.error("주 평균 사용량의 2배를 초과하는 발주는 사유를 입력해야 해요.", icon=":material/error:")
                else:
                    st.session_state["po_cart"].append(
                        {
                            "item_code": po_item_code,
                            "item_name": po_item_name,
                            "camp": po_camp,
                            "qty": int(po_qty),
                            "weekly_avg": po_camp_avg,
                            "reason": po_reason.strip(),
                        }
                    )
                    # 담기에 성공했을 때만 입력칸을 비운다 (검증 실패 시에는 입력한 값이 유지돼야 함)
                    st.session_state.pop(f"po_qty_{po_item_code}", None)
                    st.session_state.pop(f"po_reason_{po_item_code}", None)
                    st.rerun()

    if st.session_state["po_cart"]:
        st.subheader(f"담긴 발주 목록 ({len(st.session_state['po_cart'])}건)")
        for i, row in enumerate(st.session_state["po_cart"]):
            cc0, cc1, cc2 = st.columns([1, 5, 1])
            cc0.badge(str(i + 1), color="gray")
            avg_suffix = f" (주평균 {row['weekly_avg']:g}개)" if row["weekly_avg"] else ""
            reason_suffix = f" · 사유: {row['reason']}" if row["reason"] else ""
            cc1.write(
                f"{row['item_name']} ({row['item_code']}) → {row['camp']} · {row['qty']}개{avg_suffix}{reason_suffix}"
            )
            if cc2.button("삭제", key=f"po_cart_remove_{i}"):
                st.session_state["po_cart"].pop(i)
                st.rerun()

        if st.button("전체 발주 요청 제출", type="primary", icon=":material/send:"):
            if not po_my_name.strip():
                st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
            else:
                for row in st.session_state["po_cart"]:
                    create_warehouse_order(
                        row["item_code"], row["item_name"], row["camp"], row["qty"],
                        row["weekly_avg"], row["reason"], po_my_name.strip(),
                    )
                st.session_state["po_cart"] = []
                st.success("발주 요청을 모두 등록했습니다.", icon=":material/send:")
                st.rerun()

    po_df = list_warehouse_orders()
    po_requested = po_df[po_df["status"] == "requested"] if not po_df.empty else po_df
    po_in_transit = po_df[po_df["status"] == "in_transit"] if not po_df.empty else po_df
    po_done = po_df[po_df["status"].isin(["completed", "rejected"])] if not po_df.empty else po_df

    if not po_requested.empty:
        st.markdown("**요청중**")
        for _, r in po_requested.iterrows():
            c0, c1, c2, c3, c4 = st.columns([0.5, 1, 4, 1, 1])
            c0.checkbox("", key=f"po_bulk_chk_{r['id']}", label_visibility="collapsed")
            c1.badge("요청중", icon=":material/schedule:", color="orange")
            reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
            c2.write(
                f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
                f"요청자: {r['requested_by']}{reason_suffix}"
            )
            if c3.button("승인", key=f"po_approve_{r['id']}"):
                if not po_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    approve_warehouse_order(r["id"], po_my_name.strip())
                    st.rerun()
            if c4.button("거절", key=f"po_reject_{r['id']}"):
                if not po_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    reject_warehouse_order(r["id"], po_my_name.strip())
                    st.rerun()

        po_bulk_ids = [
            int(r["id"]) for _, r in po_requested.iterrows() if st.session_state.get(f"po_bulk_chk_{r['id']}")
        ]
        if po_bulk_ids:
            if st.button(
                f"체크한 {len(po_bulk_ids)}건 일괄 승인", type="primary", icon=":material/done_all:",
                key="po_bulk_approve_btn",
            ):
                if not po_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    for oid in po_bulk_ids:
                        approve_warehouse_order(oid, po_my_name.strip())
                    st.success(f"{len(po_bulk_ids)}건을 일괄 승인했습니다.", icon=":material/done_all:")
                    st.rerun()

    if not po_in_transit.empty:
        st.markdown("**승인됨 (입고 대기)**")
        for _, r in po_in_transit.iterrows():
            c0, c1, c2 = st.columns([1, 5, 1])
            c0.badge("승인됨", icon=":material/local_shipping:", color="blue")
            c1.write(
                f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · "
                f"승인자: {r['approved_by']}"
            )
            if c2.button("입고완료", key=f"po_receive_{r['id']}"):
                if not po_my_name.strip():
                    st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                else:
                    item = find_item_by_code(data, r["item_code"])
                    if not item:
                        st.error("해당 품목을 현재 재고 데이터에서 찾을 수 없어요.", icon=":material/error:")
                    else:
                        unit_amt = (item["a"] / item["q"]) if item.get("q") else 0
                        amt = round(unit_amt * r["qty"])
                        add_camp_stock(item, r["to_camp"], int(r["qty"]), amt)
                        save_data("inventory", data)
                        complete_warehouse_order(r["id"], po_my_name.strip(), amt)
                        st.success("입고 완료 처리했습니다.", icon=":material/inventory_2:")
                        st.rerun()

    if not po_done.empty:
        with st.expander(f"완료/거절 내역 ({len(po_done)}건)", icon=":material/history:"):
            for _, r in po_done.iterrows():
                is_done = r["status"] == "completed"
                who = r["received_by"] if is_done else r["approved_by"]
                dc0, dc1 = st.columns([1, 5])
                if is_done:
                    dc0.badge("입고완료", icon=":material/check_circle:", color="green")
                else:
                    dc0.badge("거절", icon=":material/cancel:", color="red")
                reason_suffix = f" · 사유: {r['reason']}" if r.get("reason") else ""
                dc1.caption(
                    f"{r['item_name']} ({r['item_code']}) → {r['to_camp']} · {int(r['qty'])}개 · {who}{reason_suffix}"
                )

with tab_warehouse:
    if not get_boxhero_token():
        st.info(
            "박스히어로 API 토큰이 설정되지 않았어요. .streamlit/secrets.toml.example을 참고해 등록해주세요.",
            icon=":material/link_off:",
        )
    else:
        try:
            with st.spinner("박스히어로에서 창고 데이터를 가져오는 중이에요..."):
                wh_locations = fetch_boxhero_locations()
                wh_items = fetch_boxhero_items()
        except Exception as e:
            st.error(f"박스히어로 연동 중 문제가 발생했습니다: {e}", icon=":material/error:")
        else:
            warehouse_qty = sum(it.get("quantity", 0) for it in wh_items)
            warehouse_amt = sum(it.get("quantity", 0) * float(it.get("price") or 0) for it in wh_items)
            warehouse_name = wh_locations[0]["name"] if wh_locations else "물류창고"

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("창고 재고 수량", f"{fmt_int(warehouse_qty)}개", border=True)
            k2.metric("창고 재고 금액", fmt_won(warehouse_amt), border=True)
            k3.metric("창고 품목 수", f"{len(wh_items):,}종", border=True)
            k4.metric("창고", warehouse_name, border=True)
            st.caption(
                "박스히어로 API에서 5분 주기로 새로 가져온 데이터예요. "
                "창고 재고 금액은 원가(cost) 입력이 대부분 비어있어 판매가(price) 기준으로 계산했어요."
            )

            try:
                save_warehouse_snapshot(warehouse_qty, warehouse_amt, len(wh_items))
            except Exception:
                pass  # 스냅샷 저장에 실패해도 화면 표시는 계속 진행

            _cur_period_label = current_inventory_period()[2]
            if st.session_state.get("_wh_value_snap_period") != _cur_period_label:
                try:
                    save_inventory_value_snapshot("warehouse", build_warehouse_value_snapshot_rows(wh_items))
                    st.session_state["_wh_value_snap_period"] = _cur_period_label
                except Exception:
                    pass  # 스냅샷 저장에 실패해도 화면 표시는 계속 진행

            st.subheader("월별 물류창고 재고 금액")
            snapshots = load_warehouse_snapshots()
            if snapshots.empty:
                st.caption("아직 쌓인 스냅샷이 없어요.")
            else:
                snapshots = snapshots.copy()
                snapshots["월"] = pd.to_datetime(snapshots["snapshot_date"]).dt.strftime("%Y-%m")
                monthly_wh = snapshots.groupby("월", as_index=False).last()[["월", "total_amt"]]
                monthly_wh.columns = ["월", "재고 금액"]
                st.altair_chart(render_trend_chart(monthly_wh, "월", "재고 금액"), width="stretch")
                st.dataframe(
                    monthly_wh.sort_values("월", ascending=False),
                    hide_index=True,
                    column_config={"재고 금액": st.column_config.NumberColumn(format="₩%,d")},
                )
                st.caption(
                    "박스히어로 API는 현재 시점 재고만 알려줘서, 창고 현황 탭을 열 때마다 그날의 "
                    "스냅샷을 기록해 추이를 쌓고 있어요. 과거 데이터는 없어 오늘부터 시작돼요."
                )

            st.subheader("품목별 창고 재고")
            st.caption("주 사용량/예상 소진일은 캠프 사용량 엑셀 기준(전체 캠프 합산)이라, 사용량 데이터가 없는 SKU는 \"-\"로 표시돼요.")
            wh_search = st.text_input("품목명 또는 SKU로 검색", "", key="warehouse_search")
            wh_rows = []
            for it in wh_items:
                sku = it["sku"]
                qty = it.get("quantity", 0)
                price = float(it.get("price") or 0)
                weeks, depletion_date = estimate_depletion(qty, sku)
                wh_rows.append(
                    {
                        "SKU": sku,
                        "품목명": it["name"],
                        "재고 수량": qty,
                        "단가": price,
                        "재고 금액": qty * price,
                        "주 사용량": weekly_usage_rate(sku),
                        "소진 예상(주)": weeks,
                        "예상 소진일": depletion_date.isoformat() if depletion_date else None,
                    }
                )
            wh_df = pd.DataFrame(wh_rows).sort_values("재고 금액", ascending=False)
            if wh_search.strip():
                q = wh_search.strip().lower()
                wh_df = wh_df[
                    wh_df["품목명"].str.lower().str.contains(q, regex=False)
                    | wh_df["SKU"].str.lower().str.contains(q, regex=False)
                ]
            display_wh_df = wh_df.copy()
            display_wh_df["예상 소진일"] = display_wh_df["예상 소진일"].fillna("-")
            st.dataframe(
                display_wh_df,
                hide_index=True,
                column_config={
                    "재고 수량": st.column_config.NumberColumn(format="%,d개"),
                    "단가": st.column_config.NumberColumn(format="₩%,d"),
                    "재고 금액": st.column_config.NumberColumn(format="₩%,d"),
                    "주 사용량": st.column_config.NumberColumn(format="%.1f개/주"),
                    "소진 예상(주)": st.column_config.NumberColumn(format="%.1f주"),
                },
            )

            st.subheader("최근 30일 출고 금액")
            st.caption(
                "건별 품목 상세를 하나씩 조회해서 계산하기 때문에 건수가 많으면 1~2분 걸려요. "
                "그래서 자동으로 돌리지 않고, 버튼을 눌렀을 때만 계산해요 (다른 탭 조작 속도에 영향 없게)."
            )
            if st.button("출고 이력 불러오기 / 새로고침", key="load_out_tx_btn", icon=":material/refresh:"):
                try:
                    with st.spinner("건별 품목 상세를 조회해 판매가 기준 출고 금액을 계산하는 중이에요... (건수가 많으면 1~2분 걸릴 수 있어요)"):
                        st.session_state["wh_out_txs"] = fetch_boxhero_recent_out_transactions(days=30)
                except Exception as e:
                    st.error(f"출고 이력을 가져오는 중 문제가 발생했습니다: {e}", icon=":material/error:")

            out_txs = st.session_state.get("wh_out_txs")
            if out_txs is None:
                st.caption("위 버튼을 눌러 최근 30일 출고 이력을 불러오세요.")
            else:
                if not out_txs:
                    st.caption("최근 30일간 출고 이력이 없어요.")
                else:
                    total_out_amt = sum(tx["amt"] for tx in out_txs)
                    st.caption(f"최근 30일 출고 금액 합계: {fmt_won(total_out_amt)} (판매가 기준, {len(out_txs):,}건)")

                    daily = {}
                    for tx in out_txs:
                        day = tx["transaction_time"][:10]
                        daily[day] = daily.get(day, 0) + tx["amt"]
                    daily_df = pd.DataFrame(sorted(daily.items()), columns=["날짜", "출고 금액"])
                    st.altair_chart(render_trend_chart(daily_df, "날짜", "출고 금액"), width="stretch")

                    tx_rows = [
                        {
                            "일시": tx["transaction_time"][:16].replace("T", " "),
                            "출고 금액": tx["amt"],
                            "출고 수량": abs(tx.get("total_quantity", 0)),
                            "품목 수": tx.get("count_of_items", 0),
                            "메모": tx.get("memo", ""),
                        }
                        for tx in out_txs
                    ]
                    st.dataframe(
                        pd.DataFrame(tx_rows),
                        hide_index=True,
                        column_config={
                            "출고 금액": st.column_config.NumberColumn(format="₩%,d"),
                            "출고 수량": st.column_config.NumberColumn(format="%,d개"),
                        },
                    )

st.divider()
st.caption("업로드한 데이터는 이 앱에 접속하는 모든 사람에게 공유됩니다. 재고 엑셀/사용량 JSON을 다시 올리면 바로 반영돼요.")