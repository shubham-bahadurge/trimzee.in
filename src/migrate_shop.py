"""Apply the Postgres shop schema migration to the configured Neon database.

Run from the repository root with DATABASE_URL (or POSTGRES_URL) set:
    python src/migrate_shop.py
"""
from pathlib import Path

import db
import pgdb

pgdb.install(db)


MIGRATIONS = [
    ("001_shop", Path(__file__).parent / "migrations" / "001_shop.sql"),
    ("002_shop_payments", Path(__file__).parent / "migrations" / "002_shop_payments.sql"),
    ("003_shop_product_images", Path(__file__).parent / "migrations" / "003_shop_product_images.sql"),
]


def apply_migration():
    if not pgdb.ENABLED:
        raise RuntimeError("Set DATABASE_URL (or POSTGRES_URL) to the Neon Postgres database first.")

    conn = db.get_db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shop_schema_migrations ("
            "migration_id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = False
        for migration_id, migration_file in MIGRATIONS:
            if conn.execute(
                    "SELECT 1 FROM shop_schema_migrations WHERE migration_id = ?",
                    (migration_id,)).fetchone():
                continue
            statements = [statement.strip() for statement in migration_file.read_text(
                encoding="utf-8").split(";") if statement.strip()]
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO shop_schema_migrations (migration_id) VALUES (?) RETURNING migration_id",
                (migration_id,)).fetchone()
            applied = True
        conn.commit()
        return applied
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        pgdb.release()


if __name__ == "__main__":
    print("Applied shop migration." if apply_migration() else "Shop migration is already applied.")
