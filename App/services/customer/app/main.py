import os
import hashlib
import hmac
import secrets
import time
from typing import Generator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, String, cast, create_engine, inspect, literal, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


INITIAL_BALANCE = 500.00
PASSWORD_HASH_ITERATIONS = 310_000


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=INITIAL_BALANCE)


class CustomerRegister(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    # Email-style usernames are supported as well as simple usernames.
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.@+-]+$")
    email: str = Field(min_length=5, max_length=255)
    phone: str | None = Field(default=None, max_length=40)
    password: str = Field(min_length=8, max_length=128)


class CustomerUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=255)
    phone: str | None = Field(default=None, max_length=40)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class ChargeRequest(BaseModel):
    amount: float = Field(gt=0)


class CustomerRead(BaseModel):
    id: int
    name: str
    username: str
    email: str
    phone: str | None
    balance: float
    model_config = ConfigDict(from_attributes=True)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _prepare_schema(engine) -> None:
    """Keep the local demo database usable after adding auth fields.

    A production deployment should use Alembic migrations. This lightweight
    compatibility step prevents an existing local Compose volume from failing
    immediately when the new columns are introduced.
    """
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("customers")}
    additions = {
        "username": "VARCHAR(80)",
        "password_hash": "VARCHAR(255)",
        "balance": "FLOAT",
    }
    missing = {name: definition for name, definition in additions.items() if name not in columns}
    if not missing:
        return

    # SQLAlchemy expressions keep the backfill portable across SQLite,
    # PostgreSQL and SQL Server (e.g. string concatenation syntax differs).
    customers = Customer.__table__
    with engine.begin() as connection:
        for name, definition in missing.items():
            # "ADD" without "COLUMN" is valid on SQLite, PostgreSQL and SQL Server.
            connection.execute(text(f"ALTER TABLE customers ADD {name} {definition}"))
        connection.execute(
            update(customers)
            .where(customers.c.username.is_(None))
            .values(username=literal("legacy_") + cast(customers.c.id, String))
        )
        connection.execute(
            update(customers)
            .where(customers.c.password_hash.is_(None))
            .values(password_hash="!legacy-login-disabled")
        )
        connection.execute(
            update(customers).where(customers.c.balance.is_(None)).values(balance=INITIAL_BALANCE)
        )
        for index in customers.indexes:
            index.create(connection, checkfirst=True)


def _setting(name: str, default: str) -> str:
    """Return NAME, or the contents of the file named by NAME_FILE.

    The *_FILE form is used on AKS, where Key Vault secrets are mounted as
    files by the Secrets Store CSI driver instead of being exposed as env vars.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    return os.getenv(name, default)


def _database_engine(database_url: str):
    if database_url.startswith("sqlite"):
        return create_engine(database_url, connect_args={"check_same_thread": False})
    # Azure SQL drops idle connections (and serverless databases auto-pause),
    # so validate pooled connections before use and recycle them periodically.
    return create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)


def create_app(database_url: str | None = None) -> FastAPI:
    database_url = database_url or _setting("DATABASE_URL", "sqlite:///./customer.db")
    engine = _database_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    _prepare_schema(engine)

    registry = CollectorRegistry()
    request_count = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["service", "method", "path", "status"],
        registry=registry,
    )
    request_latency = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["service", "method", "path"],
        registry=registry,
    )

    app = FastAPI(title="Customer Service", version="0.1.0")
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def collect_metrics(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        request_count.labels("customer", request.method, path, response.status_code).inc()
        request_latency.labels("customer", request.method, path).observe(time.perf_counter() - started)
        return response

    def get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/health/live")
    def liveness():
        return {"status": "ok", "service": "customer"}

    @app.get("/health/ready")
    def readiness(db: Session = Depends(get_db)):
        db.scalar(select(Customer.id).limit(1))
        return {"status": "ready", "service": "customer"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    def register_customer(payload: CustomerRegister, db: Session) -> Customer:
        existing = db.scalar(
            select(Customer).where(
                (Customer.username == payload.username.lower())
                | (Customer.email == payload.email.lower())
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username or email is already registered")

        customer = Customer(
            name=payload.name,
            username=payload.username.lower(),
            email=payload.email.lower(),
            phone=payload.phone,
            password_hash=_hash_password(payload.password),
            balance=INITIAL_BALANCE,
        )
        db.add(customer)
        db.commit()
        db.refresh(customer)
        return customer

    @app.post("/auth/register", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
    def register(payload: CustomerRegister, db: Session = Depends(get_db)):
        return register_customer(payload, db)

    @app.post("/customers", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
    def create_customer(payload: CustomerRegister, db: Session = Depends(get_db)):
        return register_customer(payload, db)

    @app.post("/auth/login", response_model=CustomerRead)
    def login(payload: LoginRequest, db: Session = Depends(get_db)):
        customer = db.scalar(select(Customer).where(Customer.username == payload.username.lower()))
        if customer is None or not _verify_password(payload.password, customer.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        return customer

    @app.get("/customers/{customer_id}", response_model=CustomerRead)
    def get_customer(customer_id: int, db: Session = Depends(get_db)):
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        return customer

    @app.put("/customers/{customer_id}", response_model=CustomerRead)
    def update_customer(customer_id: int, payload: CustomerUpdate, db: Session = Depends(get_db)):
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer.name = payload.name
        customer.email = payload.email.lower()
        customer.phone = payload.phone
        db.commit()
        db.refresh(customer)
        return customer

    @app.get("/customers/by-email/{email}", response_model=CustomerRead)
    def get_customer_by_email(email: str, db: Session = Depends(get_db)):
        customer = db.scalar(select(Customer).where(Customer.email == email.lower()))
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        return customer

    @app.post("/customers/{customer_id}/charge", response_model=CustomerRead)
    def charge_customer(customer_id: int, payload: ChargeRequest, db: Session = Depends(get_db)):
        customer = db.scalar(
            select(Customer).where(Customer.id == customer_id).with_for_update()
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        amount = round(payload.amount, 2)
        if customer.balance < amount:
            raise HTTPException(status_code=409, detail="Insufficient account balance")
        customer.balance = round(customer.balance - amount, 2)
        db.commit()
        db.refresh(customer)
        return customer

    @app.post("/customers/{customer_id}/refund", response_model=CustomerRead)
    def refund_customer(customer_id: int, payload: ChargeRequest, db: Session = Depends(get_db)):
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer.balance = round(customer.balance + round(payload.amount, 2), 2)
        db.commit()
        db.refresh(customer)
        return customer

    return app


app = create_app()
