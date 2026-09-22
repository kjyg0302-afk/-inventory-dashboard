-- 캠프 재고 현황판이 사용하는 저장소 스키마.
-- Supabase 프로젝트의 SQL Editor에서 한 번만 실행하면 됩니다.

create table if not exists app_data (
    key text primary key,
    data jsonb not null,
    updated_at timestamptz not null default now()
);

-- 캠프 간 재고 이관 요청/승인/입고 처리 이력
create table if not exists transfer_requests (
    id bigserial primary key,
    item_code text not null,
    item_name text not null,
    from_camp text not null,
    to_camp text not null,
    qty integer not null,
    amt numeric,
    status text not null default 'requested',  -- requested / in_transit / completed / rejected
    requested_by text not null,
    requested_at timestamptz not null default now(),
    approved_by text,
    approved_at timestamptz,
    received_by text,
    received_at timestamptz
);

create index if not exists idx_transfer_requests_item on transfer_requests (item_code);
create index if not exists idx_transfer_requests_status on transfer_requests (status);

-- 박스히어로 물류창고 재고 스냅샷 (일별 1건). 박스히어로 API는 현재 시점 재고만 주기 때문에,
-- 창고 현황 탭을 열 때마다 그날의 스냅샷을 기록해 월별 추이를 쌓아나간다.
create table if not exists warehouse_snapshots (
    snapshot_date date primary key,
    total_qty numeric not null,
    total_amt numeric not null,
    item_count integer not null,
    created_at timestamptz not null default now()
);

-- 캠프 -> 물류창고 발주 요청/승인/입고 처리 이력.
-- 승인은 "창고에서 발송 준비" 단계일 뿐, 박스히어로 실제 재고는 건드리지 않는다
-- (박스히어로 반영은 수동으로 처리). 입고완료 시에만 캠프 쪽 재고 데이터에 반영한다.
create table if not exists warehouse_orders (
    id bigserial primary key,
    item_code text not null,
    item_name text not null,
    to_camp text not null,
    qty integer not null,
    amt numeric,
    weekly_avg_usage numeric,
    reason text,
    status text not null default 'requested',  -- requested / in_transit / completed / rejected
    requested_by text not null,
    requested_at timestamptz not null default now(),
    approved_by text,
    approved_at timestamptz,
    received_by text,
    received_at timestamptz
);

create index if not exists idx_warehouse_orders_item on warehouse_orders (item_code);
create index if not exists idx_warehouse_orders_status on warehouse_orders (status);

-- 사용량 로우 데이터(관계형). 사용량 엑셀을 업로드할 때마다 전체를 새로 채운다.
-- usage 요약 JSON(app_data)이 미리 정해둔 모양(월별/주별 등)으로만 볼 수 있는 것과 달리,
-- 이 테이블은 SQL로 어떤 기준으로든 자유롭게 집계할 수 있고, 예측(forecast) 등에도 바로 쓸 수 있다.
create table if not exists usage_facts (
    id bigserial primary key,
    year integer not null,
    month integer not null,
    week integer not null,
    camp text not null,
    item_code text not null,
    item_name text,
    qty numeric not null default 0,
    amt numeric not null default 0
);

create index if not exists idx_usage_facts_item on usage_facts (item_code);
create index if not exists idx_usage_facts_camp on usage_facts (camp);
create index if not exists idx_usage_facts_year_month on usage_facts (year, month);
create index if not exists idx_usage_facts_year_week on usage_facts (year, week);

-- 캠프별 x 품목별 x 월별 수요 예측 (캠프 담당자가 직접 입력).
-- historical_avg_qty는 입력 시점의 최근 3개월 평균(참고용 스냅샷)이라 나중에 재계산해도 안 바뀐다.
-- 추후 회귀분석 기반 자동 예측으로 대체/보완할 수 있도록 값만 갱신하면 되는 구조로 둔다.
create table if not exists demand_forecasts (
    id bigserial primary key,
    camp text not null,
    item_code text not null,
    item_name text,
    forecast_year integer not null,
    forecast_month integer not null,
    predicted_qty numeric not null,
    historical_avg_qty numeric,
    entered_by text not null,
    entered_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (camp, item_code, forecast_year, forecast_month)
);

create index if not exists idx_demand_forecasts_camp_month
    on demand_forecasts (camp, forecast_year, forecast_month);
create index if not exists idx_demand_forecasts_item on demand_forecasts (item_code);
