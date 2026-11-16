"""StoreTrak - FastAPI backend (SQLite locally, PostgreSQL via DATABASE_URL online)."""
import os, io, csv, hmac, hashlib, base64, json, time, datetime as dt
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import IntegrityError

URL = os.getenv("DATABASE_URL", "sqlite:///./shop.db").replace("postgres://", "postgresql://", 1)
SECRET = os.getenv("SECRET_KEY", "change-me-in-production")
engine = create_engine(URL, pool_pre_ping=True,
                       connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {})
Session = sessionmaker(bind=engine, autoflush=False)
if URL.startswith("sqlite"):  # make SQLite enforce foreign keys like PostgreSQL does
    @event.listens_for(engine, "connect")
    def _fk(conn, rec): conn.execute("PRAGMA foreign_keys=ON")
Base = declarative_base()
now = lambda: dt.datetime.utcnow() + dt.timedelta(hours=5)  # Pakistan time

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True); username = Column(String, unique=True, nullable=False)
    pw = Column(String, nullable=False); role = Column(String, default="cashier")
class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True); name = Column(String, unique=True, nullable=False)
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True); name = Column(String, nullable=False)
    barcode = Column(String, unique=True); category = Column(String); unit = Column(String, default="pcs")
    buy_price = Column(Float, default=0); sell_price = Column(Float, default=0)
    stock = Column(Float, default=0); min_stock = Column(Float, default=10); description = Column(Text)
class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True); name = Column(String, nullable=False)
    phone = Column(String); balance = Column(Float, default=0)
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True); name = Column(String, nullable=False)
    phone = Column(String); balance = Column(Float, default=0)
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True); title = Column(String, nullable=False)
    amount = Column(Float, default=0); date = Column(DateTime, default=now)
class Purchase(Base):
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True); supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    total = Column(Float, default=0); paid = Column(Float, default=0); date = Column(DateTime, default=now)
    items = relationship("PurchaseItem", cascade="all")
class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True); purchase_id = Column(Integer, ForeignKey("purchases.id"))
    product_id = Column(Integer, ForeignKey("products.id")); qty = Column(Float); price = Column(Float)
class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True); customer_id = Column(Integer, ForeignKey("customers.id"))
    subtotal = Column(Float); discount = Column(Float, default=0); total = Column(Float)
    paid = Column(Float); cost = Column(Float); cashier = Column(String); date = Column(DateTime, default=now); status = Column(String)
    items = relationship("SaleItem", cascade="all"); customer = relationship("Customer")
class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True); sale_id = Column(Integer, ForeignKey("sales.id"))
    product_id = Column(Integer, ForeignKey("products.id")); qty = Column(Float)
    price = Column(Float); cost = Column(Float); product = relationship("Product")

MODELS = {"categories": Category, "products": Product, "suppliers": Supplier,
          "customers": Customer, "expenses": Expense}

def h(p): return hashlib.pbkdf2_hmac("sha256", p.encode(), SECRET.encode(), 100000).hex()
Base.metadata.create_all(engine)
if "status" not in [c["name"] for c in inspect(engine).get_columns("sales")]:  # upgrade older databases
    with engine.begin() as c: c.execute(text("ALTER TABLE sales ADD COLUMN status VARCHAR"))
with Session() as s:
    if not s.query(User).count():
        s.add_all([User(username="admin", pw=h("admin123"), role="admin"),
                   User(username="cashier", pw=h("cashier123"), role="cashier")])
        s.add_all([Category(name=n) for n in ("Grocery", "Beverages", "Garments", "Electronics", "Pharmacy", "Cosmetics")])
        s.commit()

app = FastAPI(title="StoreTrak")

@app.exception_handler(ValueError)
@app.exception_handler(KeyError)
def bad(r, e): return JSONResponse({"detail": "Invalid or missing value"}, 422)

def db():
    s = Session()
    try: yield s
    finally: s.close()

def sign(d):
    b = base64.urlsafe_b64encode(json.dumps(d).encode()).decode()
    return b + "." + hmac.new(SECRET.encode(), b.encode(), hashlib.sha256).hexdigest()

def verify(t):
    try:
        b, sig = t.split(".")
        if not hmac.compare_digest(sig, hmac.new(SECRET.encode(), b.encode(), hashlib.sha256).hexdigest()): return None
        d = json.loads(base64.urlsafe_b64decode(b)); return d if d["exp"] > time.time() else None
    except Exception: return None

def me(authorization: str = Header(None), token: str = None):
    d = verify((authorization or "").replace("Bearer ", "") or token or "")
    if not d: raise HTTPException(401, "Please log in")
    return d

def admin(u=Depends(me)):
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    return u

def row(o):
    return {c.name: (v.isoformat() if isinstance(v := getattr(o, c.name), dt.datetime) else v) for c in o.__table__.columns}

def model(t):
    if t not in MODELS: raise HTTPException(404, "Not found")
    return MODELS[t]

def commit(s):
    try: s.commit()
    except IntegrityError:
        s.rollback(); raise HTTPException(409, "Duplicate entry, or record is already used in transactions")

def clean(M, data):
    out = {}
    for c in M.__table__.columns:
        if c.name in data and c.name not in ("id", "balance", "date"):
            v = data[c.name]
            if isinstance(c.type, Float):
                v = float(v or 0)
                if v < 0: raise HTTPException(422, f"{c.name} cannot be negative")
            elif isinstance(v, str): v = v.strip() or None
            out[c.name] = v
    req = "title" if M is Expense else "name"
    if not out.get(req): raise HTTPException(422, f"{req} is required")
    return out

def can(t, u, m):
    if u["role"] != "admin" and not (t == "customers" and m == "POST"): raise HTTPException(403, "Admin only")

@app.post("/api/login")
def login(d: dict, s=Depends(db)):
    u = s.query(User).filter_by(username=(d.get("username") or "").strip()).first()
    if not u or not hmac.compare_digest(u.pw, h(d.get("password") or "")): raise HTTPException(401, "Wrong username or password")
    return {"token": sign({"u": u.username, "role": u.role, "exp": time.time() + 86400}), "role": u.role, "user": u.username}

@app.get("/api/r/{t}")
def lst(t: str, s=Depends(db), u=Depends(me)):
    M = model(t); return [row(o) for o in s.query(M).order_by(M.id.desc())]

@app.post("/api/r/{t}")
def add(t: str, data: dict, s=Depends(db), u=Depends(me)):
    M = model(t); can(t, u, "POST"); o = M(**clean(M, data)); s.add(o); commit(s); return row(o)

@app.put("/api/r/{t}/{i}")
def upd(t: str, i: int, data: dict, s=Depends(db), u=Depends(me)):
    M = model(t); can(t, u, "PUT"); o = s.get(M, i)
    if not o: raise HTTPException(404, "Not found")
    for k, v in clean(M, data).items(): setattr(o, k, v)
    commit(s); return row(o)

@app.delete("/api/r/{t}/{i}")
def rem(t: str, i: int, s=Depends(db), u=Depends(me)):
    M = model(t); can(t, u, "DELETE"); o = s.get(M, i)
    if not o: raise HTTPException(404, "Not found")
    s.delete(o); commit(s); return {"ok": True}

@app.post("/api/purchases")
def purchase(d: dict, s=Depends(db), u=Depends(admin)):
    sup = s.get(Supplier, int(d.get("supplier_id") or 0))
    if not sup or not d.get("items"): raise HTTPException(422, "Select a supplier and add items")
    p = Purchase(); total = 0
    for it in d["items"]:
        pr = s.get(Product, int(it["product_id"])); q = float(it["qty"]); c = float(it["price"])
        if not pr or q <= 0 or c < 0: raise HTTPException(422, "Invalid purchase item")
        pr.stock += q; pr.buy_price = c; total += q * c
        p.items.append(PurchaseItem(product_id=pr.id, qty=q, price=c))
    paid = min(max(float(d.get("paid") or 0), 0), total)
    p.supplier_id, p.total, p.paid = sup.id, total, paid
    sup.balance += total - paid
    s.add(p); s.commit(); return {"id": p.id, "total": total}

def inv(x):
    return dict(id=x.id, date=x.date.strftime("%Y-%m-%d %H:%M"), customer=x.customer.name if x.customer else None,
                subtotal=x.subtotal, discount=x.discount, total=x.total, paid=x.paid, due=x.total - x.paid, status=x.status,
                items=[dict(name=i.product.name, qty=i.qty, price=i.price) for i in x.items])

@app.post("/api/sales")
def sale(d: dict, s=Depends(db), u=Depends(me)):
    if not d.get("items"): raise HTTPException(422, "Cart is empty")
    x = Sale(cashier=u["u"]); sub = cost = 0
    for it in d["items"]:
        p = s.get(Product, int(it["product_id"])); q = float(it["qty"])
        if not p or q <= 0: raise HTTPException(422, "Invalid item")
        if q > p.stock: raise HTTPException(409, f"Only {p.stock:g} of {p.name} in stock")
        p.stock -= q; sub += q * p.sell_price; cost += q * p.buy_price
        x.items.append(SaleItem(product_id=p.id, qty=q, price=p.sell_price, cost=p.buy_price))
    disc = min(max(float(d.get("discount") or 0), 0), sub); total = sub - disc
    paid = total if d.get("paid") in (None, "") else min(max(float(d["paid"]), 0), total)
    x.subtotal, x.discount, x.total, x.paid, x.cost = sub, disc, total, paid, cost
    cust = s.get(Customer, int(d["customer_id"])) if d.get("customer_id") else None
    if total - paid > 0.005:
        if not cust: raise HTTPException(422, "Select a customer to record udhaar")
        cust.balance += total - paid
    x.customer_id = cust.id if cust else None
    s.add(x); s.commit(); return inv(x)

@app.post("/api/pay/{t}/{i}")
def pay(t: str, i: int, d: dict, s=Depends(db), u=Depends(me)):
    if t not in ("customers", "suppliers"): raise HTTPException(404, "Not found")
    if t == "suppliers" and u["role"] != "admin": raise HTTPException(403, "Admin only")
    o = s.get(MODELS[t], i); a = float(d.get("amount") or 0)
    if not o: raise HTTPException(404, "Not found")
    if a <= 0 or a > o.balance + 0.005: raise HTTPException(422, "Amount must be between 0 and the outstanding balance")
    o.balance -= a; s.commit(); return row(o)

@app.get("/api/sales")
def sales_list(s=Depends(db), u=Depends(me)):
    q = s.query(Sale).order_by(Sale.id.desc())
    if u["role"] != "admin": q = q.filter(Sale.cashier == u["u"])
    return [{**inv(x), "cashier": x.cashier} for x in q.limit(200)]

@app.post("/api/sales/{i}/return")
def sale_return(i: int, s=Depends(db), u=Depends(admin)):
    x = s.get(Sale, i)
    if not x: raise HTTPException(404, "Sale not found")
    if x.status == "returned": raise HTTPException(409, "This sale was already returned")
    for it in x.items: it.product.stock += it.qty
    if x.customer: x.customer.balance = max(0, x.customer.balance - (x.total - x.paid))
    x.status = "returned"; s.commit(); return inv(x)

@app.get("/api/purchases")
def purchase_list(s=Depends(db), u=Depends(admin)):
    return [dict(id=p.id, date=p.date.strftime("%Y-%m-%d %H:%M"), total=p.total, paid=p.paid, items=len(p.items),
                 supplier=(s.get(Supplier, p.supplier_id).name if p.supplier_id else ""))
            for p in s.query(Purchase).order_by(Purchase.id.desc()).limit(100)]

@app.get("/api/report")
def report(period: str = "day", s=Depends(db), u=Depends(admin)):
    days = {"day": 0, "week": 6, "month": 29, "year": 364}.get(period, 0)
    today = now().replace(hour=0, minute=0, second=0, microsecond=0); since = today - dt.timedelta(days=days)
    live = lambda q: [x for x in q if x.status != "returned"]
    S = live(s.query(Sale).filter(Sale.date >= since)); P = s.query(Product).all()
    week = live(s.query(Sale).filter(Sale.date >= today - dt.timedelta(days=6)))
    sales = sum(x.total for x in S); cogs = sum(x.cost for x in S)
    exp = sum(x.amount or 0 for x in s.query(Expense).filter(Expense.date >= since))
    agg = {}
    for x in S:
        for i in x.items:
            a = agg.setdefault(i.product.name, [0, 0]); a[0] += i.qty; a[1] += i.qty * i.price
    d7 = [today - dt.timedelta(days=k) for k in range(6, -1, -1)]
    return dict(sales=sales, cogs=cogs, gross=sales - cogs, expenses=exp, net=sales - cogs - exp,
                purchases=sum(x.total for x in s.query(Purchase).filter(Purchase.date >= since)),
                cash=sum(x.paid for x in S), credit=sum(x.total - x.paid for x in S),
                outstanding=sum(c.balance for c in s.query(Customer)), payable=sum(c.balance for c in s.query(Supplier)),
                stock_value=sum(p.stock * p.buy_price for p in P), low=[row(p) for p in P if p.stock <= p.min_stock],
                trend=[dict(d=d.strftime("%a %d"), v=sum(x.total for x in week if x.date.date() == d.date())) for d in d7],
                top=[dict(name=k, qty=v[0], amt=v[1]) for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])[:5]])

@app.get("/api/export/{t}")
def export(t: str, s=Depends(db), u=Depends(admin)):
    M = {**MODELS, "sales": Sale, "purchases": Purchase}.get(t)
    if not M: raise HTTPException(404, "Not found")
    f = io.StringIO(); w = csv.DictWriter(f, fieldnames=[c.name for c in M.__table__.columns])
    w.writeheader(); w.writerows([row(o) for o in s.query(M)])
    return StreamingResponse(iter([f.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={t}.csv"})

@app.get("/")
def home(): return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
