import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, ForeignKey, inspect, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from dotenv import load_dotenv

load_dotenv()

# Railway provides DATABASE_URL as postgres:// or postgresql://. Pin the driver to
# psycopg (v3) explicitly: SQLAlchemy 2.1 switched the bare postgresql:// default
# from psycopg2 to psycopg, so relying on the default breaks across upgrades.
_url = os.getenv('DATABASE_URL', 'sqlite:///flow.db')
for _prefix in ('postgres://', 'postgresql://'):
    if _url.startswith(_prefix):
        _url = 'postgresql+psycopg://' + _url[len(_prefix):]
        break

engine = create_engine(_url, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()


class SweepAlert(Base):
    """A single flagged options contract detected during a scan."""
    __tablename__ = 'sweep_alerts'

    id               = Column(Integer, primary_key=True)
    ticker           = Column(String, nullable=False, index=True)
    option_ticker    = Column(String)                       # OCC symbol, e.g. "O:AAPL260117C00150000"
    expiry           = Column(String, nullable=False)       # e.g. "2025-01-17"
    strike           = Column(Float,  nullable=False)
    option_type      = Column(String, nullable=False)       # "call" or "put"
    volume           = Column(Integer)
    volume_delta     = Column(Integer)                      # new contracts since previous scan (burst size)
    side             = Column(String)                       # "buy"/"sell"/"mid" — trade vs quote (needs quote entitlement)
    open_interest    = Column(Integer)
    vol_oi_ratio     = Column(Float)                        # volume / open_interest
    estimated_premium = Column(Float)                       # notional USD (vol * ask * 100)
    ask              = Column(Float)
    bid              = Column(Float)
    underlying_price = Column(Float)
    implied_volatility = Column(Float)                      # from Massive greeks/IV
    delta            = Column(Float)
    gamma            = Column(Float)
    theta            = Column(Float)
    vega             = Column(Float)
    detected_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                              index=True)
    on_watchlist     = Column(Boolean, default=False)

    watchlist_entry = relationship(
        'WatchlistEntry', back_populates='alert', uselist=False
    )


class WatchlistEntry(Base):
    """A contract the user has chosen to monitor after it was flagged."""
    __tablename__ = 'watchlist_entries'

    id               = Column(Integer, primary_key=True)
    alert_id         = Column(Integer, ForeignKey('sweep_alerts.id'))
    ticker           = Column(String, nullable=False)
    option_ticker    = Column(String)                       # OCC symbol for single-contract refresh
    expiry           = Column(String, nullable=False)
    strike           = Column(Float,  nullable=False)
    option_type      = Column(String, nullable=False)
    entry_price      = Column(Float)   # ask at time of adding to watchlist
    entry_underlying = Column(Float)   # underlying price at time of adding
    added_at         = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active        = Column(Boolean, default=True)

    alert     = relationship('SweepAlert', back_populates='watchlist_entry')
    snapshots = relationship('PriceSnapshot', back_populates='entry',
                             order_by='PriceSnapshot.captured_at')


class ContractVolumeState(Base):
    """
    Last-seen cumulative day volume per contract, updated every scan.
    Lets the scanner compute the *delta* since the previous scan — Massive's
    day volume is cumulative, so the delta is what identifies a fresh burst.
    Rows from previous trading days are purged at the start of each scan.
    """
    __tablename__ = 'contract_volume_state'

    id            = Column(Integer, primary_key=True)
    option_ticker = Column(String, nullable=False, unique=True, index=True)
    day           = Column(String, nullable=False)   # ET trading date "YYYY-MM-DD"
    volume        = Column(Integer, default=0)
    updated_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))


class PriceSnapshot(Base):
    """Periodic price capture for a watchlist entry, used to track P&L over time."""
    __tablename__ = 'price_snapshots'

    id               = Column(Integer, primary_key=True)
    entry_id         = Column(Integer, ForeignKey('watchlist_entries.id'), index=True)
    option_price     = Column(Float)
    underlying_price = Column(Float)
    pnl_pct          = Column(Float)   # % change vs entry_price
    captured_at      = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                              index=True)

    entry = relationship('WatchlistEntry', back_populates='snapshots')


# Columns added after the original release, keyed by table name. init_db() adds
# any that are missing so existing SQLite/Postgres databases upgrade in place.
_ADDED_COLUMNS = {
    "sweep_alerts": {
        "option_ticker":      "VARCHAR",
        "implied_volatility": "FLOAT",
        "delta":              "FLOAT",
        "gamma":              "FLOAT",
        "theta":              "FLOAT",
        "vega":               "FLOAT",
        "volume_delta":       "INTEGER",
        "side":               "VARCHAR",
    },
    "watchlist_entries": {
        "option_ticker": "VARCHAR",
    },
}


def _ensure_columns():
    """Idempotently add any columns missing from an existing database."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all already made it with the full schema
            have = {c["name"] for c in inspector.get_columns(table)}
            for name, coltype in columns.items():
                if name not in have:
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {name} {coltype}'
                    ))


# Indexes on hot columns (names match SQLAlchemy's index=True convention so
# create_all and this migration never fight). create_all only builds indexes
# when it creates the table, so existing databases need them added here.
_INDEXES = [
    ("ix_sweep_alerts_ticker",         "sweep_alerts",      "ticker"),
    ("ix_sweep_alerts_detected_at",    "sweep_alerts",      "detected_at"),
    ("ix_price_snapshots_entry_id",    "price_snapshots",   "entry_id"),
    ("ix_price_snapshots_captured_at", "price_snapshots",   "captured_at"),
]


def _ensure_indexes():
    """Idempotently create indexes missing from an existing database."""
    with engine.begin() as conn:
        for name, table, column in _INDEXES:
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})'
            ))


def init_db():
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_indexes()
