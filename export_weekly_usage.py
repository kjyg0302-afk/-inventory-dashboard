"""
scm_data.db (actual_weekly_detail 테이블)에서 부품번호 x 캠프 x 주 단위 사용량을 뽑아
재고 대시보드(inventory-dashboard.jsx)에 업로드할 수 있는 JSON 파일로 내보냅니다.

사용법:
    python export_weekly_usage.py [db_path] [output_path]

기본값:
    db_path     = scm_data.db (현재 폴더)
    output_path = weekly_usage.json (현재 폴더)

매주/매일 실적을 갱신하신 뒤 이 스크립트를 다시 실행하면 최신 weekly_usage.json이
새로 생성됩니다. 이 파일을 재고 대시보드의 "사용량 데이터 갱신" 버튼으로 업로드하면
전체 접속자에게 반영됩니다.
"""

import sqlite3
import json
import sys
from collections import defaultdict

# 재고 목록의 캠프명과 다르게 기록된 이름을 합쳐주는 규칙.
# 필요에 따라 이 딕셔너리만 수정하면 됩니다.
CAMP_NAME_MAP = {
    "부산캠프": "부산2캠프",
    "부산정비": "부산2캠프",
    "광주정비": "광주1캠프",
    "대구정비": "대구1캠프",
    "중앙정비허브": "천안캠프",
}

# 재고/재분배와 무관해 완전히 제외할 이름
EXCLUDE_CAMPS = {"김만수(가맹임대)영천"}


def export(db_path: str, output_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT year, week_number, camp_name, part_no, total_qty "
        "FROM actual_weekly_detail"
    )
    rows = cur.fetchall()
    conn.close()

    agg = defaultdict(float)           # (part_no, camp, year, week) -> qty
    camp_all_weeks = defaultdict(set)  # camp -> {(year, week), ...}

    for year, week, camp, part_no, qty in rows:
        if not camp or camp in EXCLUDE_CAMPS or not part_no:
            continue
        part_no = part_no.strip()
        camp = CAMP_NAME_MAP.get(camp, camp)
        qty = qty or 0
        agg[(part_no, camp, year, week)] += qty
        camp_all_weeks[camp].add((year, week))

    camp_week_count = {c: len(w) for c, w in camp_all_weeks.items()}

    by_part = defaultdict(lambda: defaultdict(list))
    for (part_no, camp, year, week), qty in agg.items():
        if qty == 0:
            continue
        by_part[part_no][camp].append([year, week, qty])

    items = {}
    for part_no, camps in by_part.items():
        camp_out = {}
        for camp, wlist in camps.items():
            wlist.sort(key=lambda w: (w[0], w[1]))
            total = sum(w[2] for w in wlist)
            denom = camp_week_count.get(camp, 1) or 1
            avg = round(total / denom, 2)
            camp_out[camp] = {"w": wlist, "t": round(total, 2), "avg": avg}
        items[part_no] = camp_out

    all_weeks = sorted({(y, w) for (_, _, y, w) in agg.keys()})

    out = {
        "campWeekCount": camp_week_count,
        "weekRange": {
            "from": list(all_weeks[0]) if all_weeks else None,
            "to": list(all_weeks[-1]) if all_weeks else None,
            "count": len(all_weeks),
        },
        "items": items,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"완료: {len(items)}개 부품, {len(camp_week_count)}개 캠프 -> {output_path}")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "scm_data.db"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "weekly_usage.json"
    export(db_path, output_path)
