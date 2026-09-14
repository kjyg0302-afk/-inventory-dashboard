-- 캠프 재고 현황판이 사용하는 저장소 스키마.
-- Supabase 프로젝트의 SQL Editor에서 한 번만 실행하면 됩니다.

create table if not exists app_data (
    key text primary key,
    data jsonb not null,
    updated_at timestamptz not null default now()
);
