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
            id,
            system_code,
            name,
            description,
            creator_id,
            sport,
            visibility,
            status,
            followers,
            created_at,
            updated_at
        FROM systems
        WHERE visibility = 'public'
          AND status = 'active'
        ORDER BY created_at DESC
    """)