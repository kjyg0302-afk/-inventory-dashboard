"""숫자 포맷 및 차트 렌더링 공용 유틸리티."""

from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd

# 한국은 서머타임이 없어서 고정 오프셋으로 충분하다 (윈도우 로컬엔 tzdata가 없어 ZoneInfo가 실패할 수 있음).
KST = timezone(timedelta(hours=9))


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


def _to_kst(ts):
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(KST)


def fmt_request_timeline(r):
    """이관/발주 요청 한 건의 요청일·승인(거절)일·입고일과 경과(소요)일을 한 줄로.
    진행중(요청중/이동중)이면 요청일로부터 오늘까지 경과일, 입고완료면 요청→입고 소요일."""
    status = r.get("status")
    today = datetime.now(KST).date()
    req, appr, recv = (_to_kst(r.get(k)) for k in ("requested_at", "approved_at", "received_at"))

    def md(ts):
        return ts.strftime("%m/%d") if ts.year == today.year else ts.strftime("%Y/%m/%d")

    parts = []
    if req is not None:
        parts.append(f"요청 {md(req)}")
    if appr is not None:
        parts.append(f"{'거절' if status == 'rejected' else '승인'} {md(appr)}")
    if recv is not None:
        parts.append(f"입고 {md(recv)}")
    if req is not None:
        if status in ("requested", "in_transit"):
            parts.append(f"경과 {(today - req.date()).days}일")
        elif status == "completed" and recv is not None:
            parts.append(f"소요 {(recv.date() - req.date()).days}일")
    return " · ".join(parts)


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
