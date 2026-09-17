"""
캠프 재고 현황판 (Streamlit)
- 태블로에서 받은 재고 엑셀(피벗 형식)과, scm_data.db에서 export_weekly_usage.py로
  뽑은 주단위 사용량 JSON(또는 사용량 엑셀)을 업로드하면 대시보드가 채워집니다.
- 업로드한 데이터는 Supabase(Postgres) DB에 저장되어, 다시 접속하는 모든 사람에게
  그대로 보입니다. 앱이 잠들었다 깨어나거나 재배포되어도 DB에 저장된 데이터는
  유지됩니다. (연결 설정은 .streamlit/secrets.toml.example, schema.sql 참고)
"""

import json
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="캠프 재고 현황판", layout="wide")

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


def list_transfer_requests(item_code):
    """특정 품목의 이관 요청 이력을 최신순으로 반환."""
    conn = get_db_connection()
    return conn.query(
        "select * from transfer_requests where item_code = :item_code order by requested_at desc",
        params={"item_code": item_code},
        ttl=0,
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


def fmt_int(n):
    return f"{round(n or 0):,}"


def fmt_won(n):
    return f"₩{round(n or 0):,}"


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
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:2px;">
            <div style="width:40px;height:40px;border-radius:10px;
                        background:linear-gradient(135deg, rgba(91,141,239,0.28), rgba(91,141,239,0.06));
                        border:1px solid rgba(91,141,239,0.35);display:flex;align-items:center;
                        justify-content:center;font-size:19px;">📦</div>
            <div style="font-size:21px;font-weight:700;letter-spacing:-0.01em;">캠프 재고 현황판</div>
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


def get_usage_for_code(code):
    if not usage or not code:
        return None
    return usage["items"].get(str(code).strip())


# ---------------- 탭 ----------------

tab_overview, tab_camps, tab_items, tab_rebalance, tab_usage_amount = st.tabs(
    [
        ":material/dashboard: 개요",
        ":material/location_on: 캠프별 현황",
        ":material/search: 품목 검색",
        ":material/sync_alt: 재분배 도우미",
        ":material/payments: 월별 사용 금액",
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
    sort_col = st.selectbox("정렬 기준", ["재고 금액", "재고 수량", "캠프", "팀"], index=0)
    ascending = st.checkbox("오름차순", value=False)
    st.dataframe(
        camp_df.sort_values(sort_col, ascending=ascending),
        hide_index=True,
        column_config={
            "재고 수량": st.column_config.NumberColumn(format="%d개"),
            "재고 금액": st.column_config.NumberColumn(format="₩%d"),
        },
    )

with tab_items:
    search = st.text_input("부품명 또는 부품 번호로 검색", "")
    items = data["items"]
    if search.strip():
        q = search.strip().lower()
        items = [it for it in items if q in it["n"].lower() or q in str(it["c"]).lower()]
    st.caption(f"{len(items):,}개 품목 중 최대 50개 표시")
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
                c0, c1, c2, c3 = st.columns([1, 4, 1, 1])
                c0.badge("요청중", icon=":material/schedule:", color="orange")
                c1.write(f"{r['from_camp']} → {r['to_camp']} · {int(r['qty'])}개 · 요청자: {r['requested_by']}")
                if c2.button("승인", key=f"approve_{r['id']}"):
                    if not my_name.strip():
                        st.error("내 이름을 먼저 입력해주세요.", icon=":material/error:")
                    else:
                        item = find_item_by_code(data, item_code)
                        current_qty = item["x"].get(r["from_camp"], [0, 0])[0]
                        already_out = int(
                            req_df[
                                (req_df["status"] == "in_transit") & (req_df["from_camp"] == r["from_camp"])
                            ]["qty"].sum()
                        )
                        available = current_qty - already_out
                        if available < r["qty"]:
                            st.error(
                                f"{r['from_camp']}의 가용재고가 부족합니다. "
                                f"(가용 {available}개, 요청 {int(r['qty'])}개)",
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

        display_df = df_rows.copy()
        display_df["주 평균 사용량"] = display_df["주 평균 사용량"].apply(
            lambda v: "-" if pd.isna(v) else f"{v:g}개/주"
        )
        display_df["소진 예상(주)"] = display_df["소진 예상(주)"].apply(
            lambda v: "-" if pd.isna(v) else f"{v:g}주"
        )
        st.dataframe(
            display_df,
            hide_index=True,
            column_config={
                "재고 수량": st.column_config.NumberColumn(format="%d개"),
                "가용재고": st.column_config.NumberColumn(format="%d개"),
                "이동중재고": st.column_config.NumberColumn(format="%d개"),
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
        monthly_avg = monthly_total.mean()

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
        st.caption("가장 최근 달은 아직 마감 전이라 다른 달보다 금액이 낮게 보일 수 있어요.")

        st.subheader("전체 캠프 합산 · 월별 사용 금액 추이")
        overall_trend_df = monthly_total.reset_index()
        overall_trend_df.columns = ["월", "금액"]
        st.altair_chart(render_trend_chart(overall_trend_df, "월", "금액"), width="stretch")
        st.dataframe(
            overall_trend_df.sort_values("월", ascending=False),
            hide_index=True,
            column_config={"금액": st.column_config.NumberColumn(format="₩%d")},
        )

        camp_totals = (
            amt_df.groupby("캠프")["금액"].sum().sort_values(ascending=False).reset_index()
        )
        camp_totals.columns = ["캠프", "총 사용 금액"]

        st.subheader("캠프별 사용 금액")
        picked_camp = st.selectbox("캠프 선택", ["전체 캠프 합산"] + camp_totals["캠프"].tolist())
        if picked_camp != "전체 캠프 합산":
            camp_series = (
                amt_df[amt_df["캠프"] == picked_camp].set_index("월")["금액"].reindex(months, fill_value=0)
            )
            cc1, cc2 = st.columns(2)
            cc1.metric(f"{picked_camp} 총 사용 금액", fmt_won(camp_series.sum()), border=True)
            cc2.metric(f"{picked_camp} 월평균 사용 금액", fmt_won(camp_series.mean()), border=True)
            camp_trend_df = camp_series.reset_index()
            camp_trend_df.columns = ["월", "금액"]
            st.altair_chart(render_trend_chart(camp_trend_df, "월", "금액"), width="stretch")
            st.dataframe(
                camp_trend_df.sort_values("월", ascending=False),
                hide_index=True,
                column_config={"금액": st.column_config.NumberColumn(format="₩%d")},
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
            column_config={"총 사용 금액": st.column_config.NumberColumn(format="₩%d")},
        )

st.divider()
st.caption("업로드한 데이터는 이 앱에 접속하는 모든 사람에게 공유됩니다. 재고 엑셀/사용량 JSON을 다시 올리면 바로 반영돼요.")