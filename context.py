"""탭 렌더 함수들이 공통으로 받는 파생 데이터 묶음.
app.py가 app.py 자신을 import하지 않고도 tabs/*.py가 이 타입을 쓸 수 있도록 별도 파일로 둔다
(app.py가 tabs/*를 import하므로, 반대 방향 import는 순환 참조가 된다)."""

from dataclasses import dataclass


@dataclass
class TabContext:
    data: dict
    usage: dict | None
    auth: dict
    my_camp: str | None
    my_login_camp: str | None
    camp_df: object
    team_df: object
    grand_qty: int
    grand_amt: int
    zero_camps: list
