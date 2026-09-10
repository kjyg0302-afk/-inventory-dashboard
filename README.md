# 캠프 재고 현황판 (Streamlit)

태블로에서 받은 재고 엑셀과, `scm_data.db`에서 `export_weekly_usage.py`로 뽑은
주단위 사용량 JSON을 업로드하면 동작하는 재고/재분배 대시보드입니다.

## 로컬에서 테스트

```
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 http://localhost:8501 로 열립니다.

## GitHub + Streamlit Community Cloud로 공개 배포

1. 이 폴더를 GitHub 저장소에 올립니다 (새 저장소를 만들거나, 기존 SCM 웹 툴
   저장소 안에 `inventory-dashboard/` 같은 하위 폴더로 넣어도 됩니다).
2. https://share.streamlit.io 접속 → GitHub 계정으로 로그인
3. "New app" → 방금 올린 저장소/브랜치 선택 → Main file path에 `app.py` 지정
   (하위 폴더에 넣었다면 `inventory-dashboard/app.py` 처럼 경로 지정)
4. Deploy 클릭 → 몇 분 후 `https://<앱이름>.streamlit.app` 형태의 공개 링크 생성
   → 로그인 없이 누구나 접속 가능합니다.

## 데이터 갱신

- 배포된 링크에 접속해서 화면 상단의 "재고 엑셀로 갱신" / "사용량 데이터 갱신"
  버튼으로 파일을 업로드하면, 그 순간부터 접속하는 모든 사람에게 반영됩니다.
- 단, Streamlit Cloud는 앱을 재배포(코드 수정 후 git push)하면 로컬 저장 파일
  (`data/inventory.json`, `data/usage.json`)이 초기화될 수 있습니다. 코드를
  수정할 때마다 데이터를 다시 업로드해야 할 수 있다는 뜻이에요. 이 부분은
  나중에 Supabase 등 진짜 데이터베이스로 옮기면 해결됩니다 (SCM 웹 툴 본체와
  통합할 때 함께 처리 예정).

## 사용량 데이터 갱신 (로컬)

```
python export_weekly_usage.py scm_data.db weekly_usage.json
```

새로 생성된 `weekly_usage.json`을 배포된 앱의 "사용량 데이터 갱신" 버튼으로
업로드하세요.
