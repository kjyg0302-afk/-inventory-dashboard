"""캠프 로그인 인증. 개인별 계정이 아니라, 캠프 하나당 비밀번호 하나를 공유하는 가벼운 방식."""

import hashlib
import uuid

from sqlalchemy import text

from db import get_db_connection


def hash_password(password):
    salt = uuid.uuid4().hex
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(password, stored_hash):
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, digest = stored_hash.split("$", 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == digest


def list_camp_credential_names():
    conn = get_db_connection()
    df = conn.query("select camp from camp_credentials order by camp", ttl=30)
    return df["camp"].tolist()


def get_camp_password_hash(camp):
    conn = get_db_connection()
    df = conn.query(
        "select password_hash from camp_credentials where camp = :camp", params={"camp": camp}, ttl=0
    )
    return df.iloc[0]["password_hash"] if not df.empty else None


def set_camp_password(camp, password):
    conn = get_db_connection()
    with conn.session as session:
        session.execute(
            text(
                """
                insert into camp_credentials (camp, password_hash) values (:camp, :hash)
                on conflict (camp) do update set password_hash = excluded.password_hash, updated_at = now()
                """
            ),
            {"camp": camp, "hash": hash_password(password)},
        )
        session.commit()
