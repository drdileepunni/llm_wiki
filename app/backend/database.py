from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime
from .config import DB_PATH

engine = create_engine(f"sqlite:///{DB_PATH}")
Session = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class TokenLog(Base):
    __tablename__ = "token_log"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    operation = Column(String)       # 'ingest' | 'chat' | 'lint'
    source_name = Column(String)     # file name or query snippet
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost_usd = Column(Float)
    model = Column(String)
    kb_name = Column(String, default="default")

def init_db():
    Base.metadata.create_all(engine)
    # Migration: add kb_name to existing databases that predate this column
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE token_log ADD COLUMN kb_name VARCHAR DEFAULT 'default'"))
            conn.commit()
        except Exception:
            pass  # Column already exists
