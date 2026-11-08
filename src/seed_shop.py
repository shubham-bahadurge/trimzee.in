"""Seed the Neon shop with sample categories and products.

Run after src/migrate_shop.py, from the repository root:
    python src/seed_shop.py
"""
import db
import pgdb

pgdb.install(db)

CATEGORIES = [
    ("Hair Care", "hair-care", 10),
    ("Skin Care", "skin-care", 20),
    ("Grooming", "grooming", 30),
]

PRODUCTS = [
    ("Nourishing Shampoo", "nourishing-shampoo", "Gentle daily shampoo for clean, refreshed hair.",
     "hair-care", 34900, 49900, 25),
    ("Repair Conditioner", "repair-conditioner", "A smooth, lightweight conditioner for everyday care.",
     "hair-care", 39900, 54900, 20),
    ("Hydrating Face Wash", "hydrating-face-wash", "A mild face wash for a fresh, comfortable feel.",
     "skin-care", 24900, 29900, 30),
    ("Daily Moisturizer", "daily-moisturizer", "A simple daily moisturizer for soft, hydrated skin.",
     "skin-care", 44900, 59900, 18),
    ("Beard Care Oil", "beard-care-oil", "A conditioning grooming oil for a softer-looking beard.",
     "grooming", 29900, 39900, 22),
]


def seed():
    if not pgdb.ENABLED:
        raise RuntimeError("Set DATABASE_URL (or POSTGRES_URL) to the Neon Postgres database first.")
    conn = db.get_db()
    try:
        for name, slug, sort_order in CATEGORIES:
            conn.execute(
                "INSERT INTO categories (name, slug, sort_order, is_active) VALUES (?, ?, ?, TRUE) "
                "ON CONFLICT (slug) DO NOTHING RETURNING id", (name, slug, sort_order)).fetchone()
        category_ids = {row["slug"]: row["id"] for row in conn.execute(
            "SELECT id, slug FROM categories WHERE slug IN (?, ?, ?)",
            tuple(category[1] for category in CATEGORIES))}
        for name, slug, description, category_slug, price, mrp, stock in PRODUCTS:
            conn.execute(
                "INSERT INTO products (name, slug, description, category_id, price_paise, mrp_paise, "
                "stock_qty, track_stock, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, TRUE, TRUE) "
                "ON CONFLICT (slug) DO NOTHING RETURNING id",
                (name, slug, description, category_ids[category_slug], price, mrp, stock)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        pgdb.release()


if __name__ == "__main__":
    seed()
    print("Sample shop categories and products are ready.")
