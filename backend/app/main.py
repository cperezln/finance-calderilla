import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import date as DateValue
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, String, UniqueConstraint, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///calderilla.db"))


class Base(DeclarativeBase):
    pass


class Ledger(Base):
    __tablename__ = "ledgers"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), default="Personal")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("ledgers.id"))
    name: Mapped[str] = mapped_column(String(80), default="Calderilla")
    opening_balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)


class LedgerDefault(Base):
    __tablename__ = "ledger_defaults"
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("ledgers.id"), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), unique=True)


class Movement(Base):
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("kind IN ('expense', 'income')"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("ledgers.id"))
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(7))
    date: Mapped[DateValue] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(240))


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("ledger_id", "normalized_name"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("ledgers.id"))
    name: Mapped[str] = mapped_column(String(40))
    normalized_name: Mapped[str] = mapped_column(String(40))


class MovementTag(Base):
    __tablename__ = "transaction_tags"
    transaction_id: Mapped[UUID] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[UUID] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class MovementIn(BaseModel):
    account_id: UUID | None = None
    amount_cents: int = Field(gt=0)
    kind: Literal["expense", "income"]
    date: DateValue = Field(default_factory=DateValue.today)
    note: str | None = Field(default=None, max_length=240)
    tags: list[str] = Field(default_factory=list, max_length=20)


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    opening_balance_cents: int = 0


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        ledger = db.scalars(select(Ledger).limit(1)).first()
        if not ledger:
            ledger = Ledger()
            db.add(ledger)
            db.flush()
        account = db.scalars(select(Account).where(Account.ledger_id == ledger.id).limit(1)).first()
        if not account:
            account = Account(ledger_id=ledger.id)
            db.add(account)
            db.flush()
        if not db.get(LedgerDefault, ledger.id):
            db.add(LedgerDefault(ledger_id=ledger.id, account_id=account.id))
        db.commit()
    yield


app = FastAPI(title="Calderilla", lifespan=lifespan)


def get_db():
    with Session(engine) as db:
        yield db


def current_ledger(db: Session) -> Ledger:
    ledger = db.scalars(select(Ledger).limit(1)).one()
    return ledger


def default_account(db: Session, ledger: Ledger) -> Account:
    default = db.get(LedgerDefault, ledger.id)
    return db.get(Account, default.account_id)


def clean_tag_names(raw_names: list[str]) -> list[str]:
    names: dict[str, str] = {}
    for raw in raw_names:
        name = " ".join(raw.split())
        if not name:
            continue
        if len(name) > 40 or "," in name or ";" in name:
            raise ValueError
        names.setdefault(name.casefold(), name)
    if len(names) > 20:
        raise ValueError
    return list(names.values())


def attach_tags(db: Session, ledger: Ledger, movement: Movement, names: list[str]):
    if not names:
        return
    normalized = [name.casefold() for name in names]
    existing = db.scalars(
        select(Tag).where(Tag.ledger_id == ledger.id, Tag.normalized_name.in_(normalized))
    ).all()
    tags = {tag.normalized_name: tag for tag in existing}
    for name in names:
        key = name.casefold()
        if key not in tags:
            tags[key] = Tag(ledger_id=ledger.id, name=name, normalized_name=key)
            db.add(tags[key])
    db.flush()
    db.add_all(MovementTag(transaction_id=movement.id, tag_id=tags[key].id) for key in normalized)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def state(db: Session = Depends(get_db)):
    ledger = current_ledger(db)
    accounts = db.scalars(select(Account).where(Account.ledger_id == ledger.id).order_by(Account.name)).all()
    movements = db.scalars(
        select(Movement).where(Movement.ledger_id == ledger.id).order_by(Movement.date.desc(), Movement.id.desc())
    ).all()
    tags = db.scalars(select(Tag).where(Tag.ledger_id == ledger.id).order_by(Tag.name)).all()
    tag_rows = db.execute(
        select(MovementTag.transaction_id, Tag.name).join(Tag, Tag.id == MovementTag.tag_id)
        .where(Tag.ledger_id == ledger.id).order_by(Tag.name)
    ).all()
    movement_tags: dict[UUID, list[str]] = {}
    for movement_id, tag_name in tag_rows:
        movement_tags.setdefault(movement_id, []).append(tag_name)
    balances = {account.id: account.opening_balance_cents for account in accounts}
    for row in movements:
        balances[row.account_id] += row.amount_cents if row.kind == "income" else -row.amount_cents
    names = {account.id: account.name for account in accounts}
    default_id = default_account(db, ledger).id
    return {
        "ledger": ledger,
        "default_account_id": default_id,
        "balance_cents": sum(balances.values()),
        "tags": [{"id": tag.id, "name": tag.name} for tag in tags],
        "accounts": [
            {"id": account.id, "name": account.name, "opening_balance_cents": account.opening_balance_cents,
             "balance_cents": balances[account.id]}
            for account in accounts
        ],
        "transactions": [
            {"id": row.id, "account_id": row.account_id, "account_name": names[row.account_id],
             "amount_cents": row.amount_cents, "kind": row.kind, "date": row.date, "note": row.note,
             "tags": movement_tags.get(row.id, [])}
            for row in movements
        ],
    }


@app.post("/api/accounts", status_code=201)
def add_account(data: AccountIn, db: Session = Depends(get_db)):
    ledger = current_ledger(db)
    name = data.name.strip()
    if not name:
        raise HTTPException(422, "Account name cannot be blank")
    accounts = db.scalars(select(Account).where(Account.ledger_id == ledger.id)).all()
    if any(account.name.casefold() == name.casefold() for account in accounts):
        raise HTTPException(409, "An account with this name already exists")
    account = Account(ledger_id=ledger.id, name=name, opening_balance_cents=data.opening_balance_cents)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.put("/api/accounts/{account_id}/default", status_code=204)
def set_default_account(account_id: UUID, db: Session = Depends(get_db)):
    ledger = current_ledger(db)
    account = db.get(Account, account_id)
    if not account or account.ledger_id != ledger.id:
        raise HTTPException(404, "Account not found")
    default = db.get(LedgerDefault, ledger.id)
    default.account_id = account.id
    db.commit()


@app.get("/api/export.csv")
def export_csv(db: Session = Depends(get_db)):
    ledger = current_ledger(db)
    accounts = db.scalars(select(Account).where(Account.ledger_id == ledger.id)).all()
    names = {account.id: account.name for account in accounts}
    movements = db.scalars(
        select(Movement).where(Movement.ledger_id == ledger.id).order_by(Movement.date, Movement.id)
    ).all()
    tag_rows = db.execute(
        select(MovementTag.transaction_id, Tag.name).join(Tag, Tag.id == MovementTag.tag_id)
        .where(Tag.ledger_id == ledger.id).order_by(Tag.name)
    ).all()
    movement_tags: dict[UUID, list[str]] = {}
    for movement_id, tag_name in tag_rows:
        movement_tags.setdefault(movement_id, []).append(tag_name)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["fecha", "tipo", "cantidad", "nota", "pendiente", "cuenta", "etiquetas"])
    for row in movements:
        writer.writerow([
            row.date.isoformat(), "gasto" if row.kind == "expense" else "ingreso",
            f"{row.amount_cents // 100}.{row.amount_cents % 100:02d}", row.note or "", "", names[row.account_id],
            ";".join(movement_tags.get(row.id, [])),
        ])
    filename = f"calderilla-{DateValue.today().isoformat()}.csv"
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def csv_cents(raw: str) -> int:
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError from error
    cents = value * 100
    if value <= 0 or cents != cents.to_integral_value() or cents > 9_000_000_000_000_000_000:
        raise ValueError
    return int(cents)


@app.post("/api/import.csv")
async def import_csv(request: Request, db: Session = Depends(get_db)):
    try:
        text = (await request.body()).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "The CSV must use UTF-8")
    reader = csv.DictReader(io.StringIO(text))
    headers = {name.strip().lower() for name in (reader.fieldnames or [])}
    if not {"fecha", "tipo", "cantidad"}.issubset(headers):
        raise HTTPException(400, "Missing fecha, tipo or cantidad columns")

    ledger = current_ledger(db)
    default = default_account(db, ledger)
    existing = db.scalars(select(Account).where(Account.ledger_id == ledger.id)).all()
    accounts = {account.name.casefold(): account for account in existing}
    imported = skipped = created_accounts = 0
    for raw_row in reader:
        row = {key.strip().lower(): (value or "") for key, value in raw_row.items() if key}
        try:
            day = DateValue.fromisoformat(row["fecha"].strip())
            kind = {"gasto": "expense", "ingreso": "income", "expense": "expense", "income": "income"}[
                row["tipo"].strip().lower()
            ]
            amount = csv_cents(row["cantidad"])
            tag_names = clean_tag_names(row.get("etiquetas", "").split(";"))
        except (KeyError, ValueError):
            skipped += 1
            continue
        account_name = row.get("cuenta", "").strip()
        account = accounts.get(account_name.casefold()) if account_name else default
        if not account:
            account = Account(ledger_id=ledger.id, name=account_name)
            db.add(account)
            db.flush()
            accounts[account_name.casefold()] = account
            created_accounts += 1
        movement = Movement(
            ledger_id=ledger.id, account_id=account.id, amount_cents=amount, kind=kind,
            date=day, note=row.get("nota", "").strip() or None,
        )
        db.add(movement)
        db.flush()
        attach_tags(db, ledger, movement, tag_names)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped, "created_accounts": created_accounts}


@app.post("/api/transactions", status_code=201)
def add_transaction(data: MovementIn, db: Session = Depends(get_db)):
    ledger = current_ledger(db)
    account = db.get(Account, data.account_id) if data.account_id else default_account(db, ledger)
    if not account or account.ledger_id != ledger.id:
        raise HTTPException(404, "Account not found")
    try:
        tag_names = clean_tag_names(data.tags)
    except ValueError:
        raise HTTPException(422, "Tags must be unique names of at most 40 characters without commas or semicolons")
    values = data.model_dump(exclude={"account_id", "tags"})
    movement = Movement(**values, ledger_id=ledger.id, account_id=account.id)
    db.add(movement)
    db.flush()
    attach_tags(db, ledger, movement, tag_names)
    db.commit()
    db.refresh(movement)
    return movement


@app.delete("/api/transactions/{movement_id}", status_code=204)
def delete_transaction(movement_id: UUID, db: Session = Depends(get_db)):
    movement = db.get(Movement, movement_id)
    if not movement:
        raise HTTPException(404, "Transaction not found")
    db.execute(delete(MovementTag).where(MovementTag.transaction_id == movement.id))
    db.delete(movement)
    db.commit()


frontend = Path(__file__).resolve().parents[2] / "frontend"
app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
