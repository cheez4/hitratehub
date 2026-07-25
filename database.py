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