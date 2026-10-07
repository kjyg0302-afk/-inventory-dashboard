"""캠프별(물류창고 포함) 구글챗 웹훅 알림.
담당자가 혼자 있는 전용 채팅방에 웹훅을 달아두면 사실상 개인 DM처럼 쓸 수 있다."""

import requests
import streamlit as st
from sqlalchemy import text

from db import get_db_connection


@st.cache_data(ttl=30)
def load_camp_chat_webhooks():
    conn = get_db_connection()
    df = conn.query("select camp, webhook_url from camp_chat_webhooks", ttl=0)
    return dict(zip(df["camp"], df["webhook_url"]))


def save_camp_chat_webhook(camp, webhook_url):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into camp_chat_webhooks (camp, webhook_url) values (:camp, :url)
                on conflict (camp) do update set webhook_url = excluded.webhook_url, updated_at = now()
                """
            ),
            {"camp": camp, "url": webhook_url},
        )
        session.commit()


def delete_camp_chat_webhook(camp):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(text("delete from camp_chat_webhooks where camp = :camp"), {"camp": camp})
        session.commit()


def send_camp_chat_notification(camp, message):
    """해당 캠프 담당자 구글챗 웹훅으로 메시지를 보낸다. 웹훅이 등록 안 돼있거나 전송에
    실패해도 조용히 넘어간다 — 알림 문제가 실제 이관/발주 처리 자체를 막으면 안 되기 때문."""
    if not camp:
        return
    webhooks = load_camp_chat_webhooks()
    url = webhooks.get(camp)
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass
