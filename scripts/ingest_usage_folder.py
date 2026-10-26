"""사용량데이터 폴더에 새로 추가된 정비 원본 엑셀을 DB에 반영한다.

이미 반영한 파일(usage_ingest_log에 같은 파일명·크기로 기록된 것)은 건너뛰고, 새 파일만 수정한
시각 순서대로 반영한다. 원드라이브 동기화 중 잠금을 피하려고 임시 복사본을 읽는다.

    python scripts/ingest_usage_folder.py            # 미리보기 (DB 변경 없음)
    python scripts/ingest_usage_folder.py --apply    # 실제 반영
"""

import argparse
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # .streamlit/secrets.toml을 찾도록

from usage_data import ingest_raw_usage, list_ingested_files  # noqa: E402

FOLDER = os.path.join(ROOT, "사용량데이터")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 반영 (없으면 미리보기만)")
    ap.add_argument("--by", default="폴더 반영 스크립트")
    args = ap.parse_args()

    done = {(r.file_name, r.file_size) for r in list_ingested_files().itertuples()}
    files = [
        os.path.join(FOLDER, f) for f in os.listdir(FOLDER)
        if f.lower().endswith((".xlsx", ".xls")) and not f.startswith("~$")
    ]
    new_files = sorted(
        (p for p in files if (os.path.basename(p), os.path.getsize(p)) not in done), key=os.path.getmtime
    )
    if not new_files:
        print("새로 반영할 파일이 없어요.")
        return

    for path in new_files:
        name = os.path.basename(path)
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "usage.xlsx")
            shutil.copyfile(path, copy)
            r = ingest_raw_usage(copy, name, args.by, os.path.getsize(path), dry_run=not args.apply)
        print(f"\n[{'반영' if args.apply else '미리보기'}] {name}")
        print(f"  정비완료 {r['date_min']} ~ {r['date_max']} · {r['dates_replaced']}일치 교체 · "
              f"새 {r['rows_saved']:,}행 (기존 {r['rows_replaced_existing']:,}행 대체)")
        print(f"  다시 계산되는 주차: {', '.join(f'{y}-{w}주' for y, w in r['weeks'])}")
        if r["no_date_rows"]:
            print(f"  정비완료 날짜가 없어 제외한 행: {r['no_date_rows']:,}")
        for s in r["skipped"]:
            print(f"  ! 교체 안 함 {s['date']}: 새 파일 {s['new']}건 < 기존 {s['existing']}건")


if __name__ == "__main__":
    main()
