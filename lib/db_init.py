import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

import mysql.connector
from mysql.connector import pooling, Error

load_dotenv()

DEFAULT_POOL_NAME = os.getenv("MYSQL_POOL_NAME", "app_pool")
DEFAULT_POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "5"))

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "mysql"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "database": os.getenv("MYSQL_DATABASE"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "autocommit": False,
    "charset": "utf8mb4",
}

_connection_pool = None


def get_connection():
    global _connection_pool

    if _connection_pool is None:
        missing = [
            k
            for k, v in DB_CONFIG.items()
            if k in ("database", "user", "password") and (v is None or v == "")
        ]
        if missing:
            raise RuntimeError(f"Missing DB config keys in env: {missing}")

        _connection_pool = pooling.MySQLConnectionPool(
            pool_name=DEFAULT_POOL_NAME,
            pool_size=DEFAULT_POOL_SIZE,
            pool_reset_session=True,
            **DB_CONFIG,
        )

    return _connection_pool.get_connection()


def _execute(cursor, sql, params=None):
    cursor.execute(sql, params or ())


def _table_exists(cursor, table_name: str) -> bool:
    _execute(cursor, "SHOW TABLES LIKE %s", (table_name,))
    return cursor.fetchone() is not None


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    _execute(
        cursor,
        "SHOW COLUMNS FROM `{}` LIKE %s".format(table_name),
        (column_name,),
    )
    return cursor.fetchone() is not None


def _add_column_if_not_exists(cursor, table_name: str, column_name: str, column_definition: str):
    if not _column_exists(cursor, table_name, column_name):
        _execute(
            cursor,
            f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}",
        )


def _count_rows(cursor, table_name: str) -> int:
    _execute(cursor, f"SELECT COUNT(*) FROM `{table_name}`")
    (count,) = cursor.fetchone()
    return int(count or 0)


def _create_table_if_not_exists(cursor):
    _execute(
        cursor,
        """
        CREATE TABLE IF NOT EXISTS `slots` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `date` DATE NOT NULL,
            `time` VARCHAR(5) NOT NULL,
            `status` INT DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    )


def _ensure_columns(cursor):
    _add_column_if_not_exists(cursor, "slots", "user_id", "INT NULL")
    _add_column_if_not_exists(cursor, "slots", "group_name", "VARCHAR(255) NULL")
    _add_column_if_not_exists(cursor, "slots", "created_by", "INT NULL")
    _add_column_if_not_exists(cursor, "slots", "subscribed_users", "TEXT NULL")
    _add_column_if_not_exists(cursor, "slots", "booking_type", "VARCHAR(100) NULL")
    _add_column_if_not_exists(cursor, "slots", "comment", "TEXT NULL")
    _add_column_if_not_exists(cursor, "slots", "contact_info", "TEXT NULL")
    _add_column_if_not_exists(cursor, "slots", "booking_id", "TEXT NULL")
    _add_column_if_not_exists(cursor, "slots", "mention", "TEXT NULL")


def _seed_slots_if_empty(cursor):
    if _count_rows(cursor, "slots") == 0:
        times = [f"{hour:02d}:00" for hour in range(0, 24)]
        today = datetime.now().date()
        rows = []
        for i in range(28):
            date_val = today + timedelta(days=i)
            for t in times:
                rows.append((date_val, t, 0))

        _execute(cursor, "SET SESSION sql_safe_updates = 0")
        cursor.executemany(
            "INSERT INTO `slots` (`date`, `time`, `status`) VALUES (%s, %s, %s)",
            rows,
        )


def init_db():
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        _create_table_if_not_exists(cursor)
        _ensure_columns(cursor)
        _seed_slots_if_empty(cursor)

        conn.commit()
    except Error:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()
