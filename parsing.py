"""업로드된 엑셀 파일(재고/사용량)을 내부 JSON 구조로 변환."""

from datetime import datetime

import pandas as pd

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
    df = df[df["고객명"] != ""]
    # 캠프명이 깨져서 숫자만 들어간 오염된 행 제외
    df = df[~df["고객명"].str.fullmatch(r"\d+")]

    df["부품번호"] = df["부품번호"].astype(str).str.strip()
    df["합계 : 수량"] = pd.to_numeric(df["합계 : 수량"], errors="coerce").fillna(0)
    df["합계 : 부품계"] = pd.to_numeric(df["합계 : 부품계"], errors="coerce").fillna(0)

    return _aggregate_usage_df(df)


def _aggregate_usage_df(df: pd.DataFrame) -> dict:
    """정리된 사용량 df(고객명/부품번호/년도/월/해당주/합계 : 수량/합계 : 부품계 컬럼,
    선택적으로 부품명 컬럼 '부품')를 usage.json과 같은 구조로 집계."""
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


def parse_usage_excel_raw(file) -> dict:
    """정비 내역 원본 엑셀(건별 로우 — 정비 1건의 부품 1종이 한 행)을 읽어
    usage.json과 같은 구조로 변환. parse_usage_excel이 받는, 태블로에서 이미
    피벗/집계된 엑셀과 달리, 여기서는 상태가 '출고완료' 또는 '정비완료'인 행만
    실제 '사용'으로 보고 집계한다."""
    cols = ["상태", "고객명", "부품번호", "수량", "부품계", "년도", "월", "해당주"]
    try:
        df = pd.read_excel(file, usecols=cols, engine="calamine")
    except Exception:
        df = pd.read_excel(file, usecols=cols, engine="openpyxl")

    missing = set(cols) - set(df.columns)
    if missing:
        raise ValueError(
            f"원본 정비 내역 엑셀 형식이 올바르지 않습니다. 다음 컬럼이 없습니다: {', '.join(missing)}"
        )

    # 부품번호가 없는 행(공임 등 재고와 무관한 항목)은 제외
    df = df[df["부품번호"].notna() & (df["부품번호"].astype(str).str.strip() != "")].copy()
    # 출고완료/정비완료만 실제 '사용'으로 집계 (정비중/접수/취소/외주는 아직 소진된 재고가 아님).
    # 부품번호가 있는 행은 상태와 무관하게 거의 전부 정비완료 날짜가 찍혀 있어서
    # 날짜 유무만으로는 구분이 안 되고, 상태값 자체로 걸러야 한다.
    df = df[df["상태"].isin(["출고완료", "정비완료"])]
    if df.empty:
        raise ValueError("출고완료/정비완료 상태의 사용량 데이터를 찾을 수 없습니다.")

    df["년도"] = pd.to_numeric(df["년도"], errors="coerce")
    df["월"] = pd.to_numeric(df["월"], errors="coerce")
    df["해당주"] = pd.to_numeric(df["해당주"], errors="coerce")
    df = df.dropna(subset=["년도", "월", "해당주"])
    # 날짜가 비어 생기는 1900년 등 오염된 행 제외
    df = df[(df["년도"] >= 2020) & (df["년도"] <= 2030)]
    if df.empty:
        raise ValueError("유효한 년도/월/해당주 값을 가진 사용량 데이터를 찾을 수 없습니다.")
    df["년도"] = df["년도"].astype(int)
    df["월"] = df["월"].astype(int)
    df["해당주"] = df["해당주"].astype(int)

    df["고객명"] = df["고객명"].astype(str).str.strip().map(lambda c: USAGE_CAMP_NAME_MAP.get(c, c))
    df = df[~df["고객명"].isin(USAGE_EXCLUDE_CAMPS)]
    df = df[df["고객명"] != ""]
    df = df[~df["고객명"].str.fullmatch(r"\d+")]

    # 부품번호 대소문자가 섞여 있어 같은 부품이 다른 SKU로 쪼개지는 것을 방지
    df["부품번호"] = df["부품번호"].astype(str).str.strip().str.upper()
    df = df.rename(columns={"수량": "합계 : 수량", "부품계": "합계 : 부품계"})
    df["합계 : 수량"] = pd.to_numeric(df["합계 : 수량"], errors="coerce").fillna(0)
    df["합계 : 부품계"] = pd.to_numeric(df["합계 : 부품계"], errors="coerce").fillna(0)

    return _aggregate_usage_df(df)


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
