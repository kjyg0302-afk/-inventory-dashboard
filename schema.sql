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
