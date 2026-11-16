# StoreTrak - Smart Shop Management & POS (FastAPI + SQLAlchemy)

## Run locally
    python -m venv .venv && .venv\Scripts\activate   # Windows
    pip install -r requirements.txt
    uvicorn main:app --reload
Open http://127.0.0.1:8000 - logins: admin / admin123 (full access), cashier / cashier123 (POS, sales, customers, view products).
Data is stored in shop.db (SQLite) locally. Change the passwords and set SECRET_KEY before real use.

## Online (free)
Set env vars DATABASE_URL (free hosted PostgreSQL) and SECRET_KEY on your host. Free hosts wipe local files, so don't rely on SQLite online.
