import os
import psycopg2
import pandas as pd

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def read_sql(query, params=None):
    conn = get_conn()
    try:
        return pd.read_sql(query, conn, params=params)
    finally:
        conn.close()

def get_systems():
    return read_sql("""
        SELECT
            s.id,
            s.system_code,
            s.name,
            s.description,
            s.creator_id,
            s.sport,
            s.visibility,
            s.status,
            COUNT(sf.id) AS followers,
            s.created_at,
            s.updated_at
        FROM systems s
        LEFT JOIN system_followers sf
            ON sf.system_id = s.id
        WHERE s.visibility = 'public'
          AND s.status = 'active'
        GROUP BY
            s.id,
            s.system_code,
            s.name,
            s.description,
            s.creator_id,
            s.sport,
            s.visibility,
            s.status,
            s.created_at,
            s.updated_at
        ORDER BY s.created_at DESC
    """)

def is_watching_system(user_id, system_id):
    df = read_sql("""
        SELECT 1
        FROM system_followers
        WHERE user_id = %s
          AND system_id = %s
        LIMIT 1
    """, (user_id, system_id))

    return not df.empty


def watch_system(user_id, system_id):
    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO system_followers (
                        user_id,
                        system_id
                    )
                    VALUES (%s, %s)
                    ON CONFLICT (system_id, user_id)
                    DO NOTHING
                """, (user_id, system_id))
    finally:
        conn.close()


def unwatch_system(user_id, system_id):
    conn = get_conn()

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM system_followers
                    WHERE user_id = %s
                      AND system_id = %s
                """, (user_id, system_id))
    finally:
        conn.close()