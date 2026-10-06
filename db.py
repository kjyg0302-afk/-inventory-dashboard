"""Supabase(Postgres) 연결 헬퍼."""

import streamlit as st

DB_CONN_NAME = "supabase_db"


def get_db_connection():
    try:
        return st.connection(DB_CONN_NAME, type="sql")
    except Exception as e:
        st.error(
            "DB 연결 정보를 찾을 수 없습니다. .streamlit/secrets.toml.example을 참고해 "
            "secrets.toml을 만들거나 Streamlit Cloud의 Secrets 설정에 등록해주세요.\n\n"
            f"({e})",
            icon=":material/error:",
        )
        st.stop()
