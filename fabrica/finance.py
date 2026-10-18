"""Local accounting with authenticated Stripe receipts and a global expense cap.

No function records revenue supplied by a user or an agent. Only a signed,
live BRL Checkout webhook can create a sale. This is operational accounting:
Stripe fees, hosting, taxes, advertising, etc. must also be entered as expenses.

Stripe references: https://docs.stripe.com/webhooks/signature and
https://docs.stripe.com/api/charges/object (amount_refunded is cumulative).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
import uuid


def init_schema(conn: sqlite3.Connection) -> None:
    """Create isolated tables without committing a caller's transaction."""
    statements = (
        """CREATE TABLE IF NOT EXISTS finance_expenses (
            id INTEGER PRIMARY KEY,
            business_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            description TEXT NOT NULL,
            month TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS finance_expenses_month
            ON finance_expenses(month)""",
        """CREATE TABLE IF NOT EXISTS finance_stripe_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            payment_intent TEXT,
            received_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS finance_payments (
            payment_intent TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            business_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            buyer_hash TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS finance_payments_business
            ON finance_payments(business_id)""",
        """CREATE TABLE IF NOT EXISTS finance_refunds (
            charge_id TEXT PRIMARY KEY,
            payment_intent TEXT NOT NULL,
            amount_refunded_cents INTEGER NOT NULL
                CHECK(amount_refunded_cents >= 0),
            charge_amount_cents INTEGER NOT NULL CHECK(charge_amount_cents > 0),
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(amount_refunded_cents <= charge_amount_cents)
        )""",
        """CREATE INDEX IF NOT EXISTS finance_refunds_payment
            ON finance_refunds(payment_intent)""",
    )
    # executescript() would implicitly commit an existing transaction.
    for statement in statements:
        conn.execute(statement)


@contextmanager
def _write_transaction(conn: sqlite3.Connection):
    """Serialize the budget check and insert, including across connections."""
    outer = conn.in_transaction
    name = "finance_" + uuid.uuid4().hex
    if outer:
        conn.execute(f"SAVEPOINT {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # A savepoint in an outer deferred transaction must get its write lock
        # before reading the budget. SQLite may return BUSY rather than permit
        # an unsafe read/modify/write if an outer snapshot is already stale.
        conn.execute("UPDATE finance_expenses SET id = id WHERE 0")
        yield
        if outer:
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.commit()
    except BaseException:
        if outer:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.rollback()
        raise


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _integer(value, field: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError(f"{field} deve ser um inteiro de centavos >= {minimum}.")
    return value


def _identifier(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError(f"{field} ausente ou inválido.")
    return value.strip()


def _object_id(value, field: str) -> str:
    if isinstance(value, dict):
        value = value.get("id")
    return _identifier(value, field)


def record_expense(
    conn: sqlite3.Connection,
    business_id: str,
    amount_cents: int,
    description: str,
    *,
    monthly_cap_cents: int = 30000,
    month: str | None = None,
) -> dict:
    """Record a positive cost if every business together stays under the cap.

    ``month`` is an accounting month (YYYY-MM); the default is the UTC month.
    The cap controls entries in this ledger, not external cards or accounts.
    """
    business_id = _identifier(business_id, "business_id")
    _integer(amount_cents, "amount_cents", 1)
    _integer(monthly_cap_cents, "monthly_cap_cents")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Informe uma descrição para a despesa.")
    month = month if month is not None else datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month deve usar o formato YYYY-MM.")
    with _write_transaction(conn):
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM finance_expenses WHERE month = ?",
            (month,),
        ).fetchone()[0]
        if spent + amount_cents > monthly_cap_cents:
            raise ValueError(
                f"Teto mensal global excedido: {spent + amount_cents} > "
                f"{monthly_cap_cents} centavos em {month}."
            )
        cursor = conn.execute(
            """INSERT INTO finance_expenses
               (business_id, amount_cents, description, month, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (business_id, amount_cents, description.strip(), month, _utc_timestamp()),
        )
        result = dict(conn.execute(
            "SELECT * FROM finance_expenses WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())
    return result


def record_actual_expense(
    conn: sqlite3.Connection,
    business_id: str,
    amount_cents: int,
    description: str,
    month: str | None = None,
) -> dict:
    """Account for an incurred cost even when the monthly budget is exceeded.

    Budget enforcement belongs before authorizing new spending. A real cost
    must always affect profit and approval, including unexpected overruns.
    """
    business_id = _identifier(business_id, "business_id")
    _integer(amount_cents, "amount_cents", 1)
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Informe uma descrição para a despesa.")
    month = month if month is not None else datetime.now(timezone.utc).strftime("%Y-%m")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month deve usar o formato YYYY-MM.")
    with _write_transaction(conn):
        cursor = conn.execute(
            """INSERT INTO finance_expenses
               (business_id, amount_cents, description, month, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (business_id, amount_cents, description.strip(), month, _utc_timestamp()),
        )
        result = dict(conn.execute(
            "SELECT * FROM finance_expenses WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())
    return result


def _payment_rows(conn: sqlite3.Connection, business_id: str | None = None):
    query = """SELECT p.*,
        MIN(p.amount_cents, COALESCE((
            SELECT SUM(r.amount_refunded_cents) FROM finance_refunds r
            WHERE r.payment_intent = p.payment_intent
        ), 0)) AS refund_cents FROM finance_payments p"""
    args = ()
    if business_id is not None:
        query += " WHERE p.business_id = ?"
        args = (business_id,)
    return conn.execute(query, args).fetchall()


def totals(conn: sqlite3.Connection, business_id: str) -> dict:
    """Return lifetime BRL totals; fully refunded buyers no longer qualify."""
    business_id = _identifier(business_id, "business_id")
    rows = _payment_rows(conn, business_id)
    gross = sum(row["amount_cents"] for row in rows)
    refunds = sum(row["refund_cents"] for row in rows)
    expenses = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM finance_expenses WHERE business_id = ?",
        (business_id,),
    ).fetchone()[0]
    buyers = {row["buyer_hash"] for row in rows if row["amount_cents"] > row["refund_cents"]}
    net = gross - refunds
    return {
        "gross_revenue_cents": gross,
        "refund_cents": refunds,
        "net_revenue_cents": net,
        "expense_cents": expenses,
        "profit_cents": net - expenses,
        "distinct_buyers": len(buyers),
        "verified_sales": sum(row["amount_cents"] > row["refund_cents"] for row in rows),
    }


def evaluate(
    conn: sqlite3.Connection,
    business_id: str,
    *,
    min_buyers: int = 3,
    min_revenue_cents: int = 10000,
    min_margin: float = 0.3,
) -> dict:
    """Approve only when every configured evidence threshold is satisfied."""
    _integer(min_buyers, "min_buyers", 1)
    _integer(min_revenue_cents, "min_revenue_cents", 1)
    if isinstance(min_margin, bool) or not isinstance(min_margin, (int, float)):
        raise ValueError("min_margin deve estar entre 0 e 1.")
    if not math.isfinite(min_margin) or not 0 <= min_margin <= 1:
        raise ValueError("min_margin deve estar entre 0 e 1.")
    result = totals(conn, business_id)
    net = result["net_revenue_cents"]
    margin = result["profit_cents"] / net if net > 0 else 0.0
    reasons = []
    if result["distinct_buyers"] < min_buyers:
        reasons.append(f"São necessários {min_buyers} compradores distintos com vendas válidas.")
    if net < min_revenue_cents:
        reasons.append(f"Receita líquida abaixo de {min_revenue_cents} centavos.")
    if net <= 0 or margin < min_margin:
        reasons.append(f"Margem abaixo de {min_margin:.0%} após as despesas registradas.")
    return {
        "approved": not reasons,
        "reasons": reasons,
        **result,
        "margin": margin,
        "min_buyers": min_buyers,
        "min_revenue_cents": min_revenue_cents,
        "min_margin": min_margin,
    }


def _verified_payload(payload: bytes, signature: str, secret: str, now: int | None) -> dict:
    if not isinstance(payload, bytes) or not isinstance(signature, str):
        raise ValueError("Webhook exige o corpo bruto em bytes e Stripe-Signature.")
    if not isinstance(secret, str) or not secret:
        raise ValueError("Segredo do webhook ausente.")
    timestamps = []
    signatures = []
    for part in signature.split(","):
        key, separator, value = part.strip().partition("=")
        if separator and key == "t":
            timestamps.append(value)
        elif separator and key == "v1":
            signatures.append(value)
    if len(timestamps) != 1 or not re.fullmatch(r"\d+", timestamps[0]) or not signatures:
        raise ValueError("Assinatura Stripe inválida.")
    timestamp = int(timestamps[0])
    current = int(time.time()) if now is None else _integer(now, "now")
    if abs(current - timestamp) > 300:
        raise ValueError("Assinatura Stripe fora da tolerância de 300 segundos.")
    signed = timestamps[0].encode("ascii") + b"." + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    # A header can contain several v1 signatures during secret rotation.
    if not any(
        re.fullmatch(r"[0-9a-fA-F]{64}", candidate)
        and hmac.compare_digest(expected, candidate.lower())
        for candidate in signatures
    ):
        raise ValueError("Assinatura Stripe inválida.")
    try:
        event = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("JSON de webhook inválido.") from exc
    if not isinstance(event, dict):
        raise ValueError("Evento Stripe inválido.")
    return event


def _buyer_hash(session: dict) -> str:
    details = session.get("customer_details")
    details = details if isinstance(details, dict) else {}
    customer = session.get("customer")
    expanded = customer if isinstance(customer, dict) else {}
    email = details.get("email") or session.get("customer_email") or expanded.get("email")
    if isinstance(email, str) and email.strip():
        # Prefer email so two Checkout-created customer IDs for the same email
        # cannot be mistaken for independent customers. Do not store the email.
        identity = "email:" + email.strip().casefold()
    else:
        identity = "customer:" + _object_id(customer, "customer ou email")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def record_stripe_event(
    conn: sqlite3.Connection,
    payload: bytes,
    signature: str,
    secret: str,
    *,
    now: int | None = None,
) -> dict:
    """Verify raw signed bytes, then idempotently book a sale or refund.

    Unsupported events are acknowledged as ignored. Unsupported currency,
    test mode, unpaid sessions, malformed data and invalid signatures raise
    ValueError. Refunds can arrive before their matching Checkout sale.
    """
    event = _verified_payload(payload, signature, secret, now)
    event_id = _identifier(event.get("id"), "event.id")
    event_type = _identifier(event.get("type"), "event.type")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if event_type not in {"checkout.session.completed", "charge.refunded"}:
        return {"status": "ignored", "event_id": event_id, "type": event_type}
    if not isinstance(obj, dict):
        raise ValueError("Objeto do evento Stripe ausente.")
    if event.get("livemode") is not True or obj.get("livemode", True) is not True:
        raise ValueError("Somente pagamentos reais (livemode=true) são aceitos.")
    if obj.get("currency") != "brl":
        raise ValueError("Somente pagamentos em BRL são aceitos.")
    payment_intent = _object_id(obj.get("payment_intent"), "payment_intent")
    received_at = _utc_timestamp()
    if event_type == "checkout.session.completed":
        if obj.get("payment_status") != "paid":
            raise ValueError("A sessão Checkout ainda não está paga.")
        metadata = obj.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("metadata.factory_business_id ausente.")
        business_id = _identifier(metadata.get("factory_business_id"), "factory_business_id")
        session_id = _identifier(obj.get("id"), "session.id")
        amount = _integer(obj.get("amount_total"), "amount_total", 1)
        buyer_hash = _buyer_hash(obj)
    else:
        charge_id = _identifier(obj.get("id"), "charge.id")
        charge_amount = _integer(obj.get("amount"), "charge.amount", 1)
        refunded = _integer(obj.get("amount_refunded"), "amount_refunded")
        if refunded > charge_amount:
            raise ValueError("Reembolso excede o valor da cobrança.")

    with _write_transaction(conn):
        if conn.execute(
            "SELECT 1 FROM finance_stripe_events WHERE event_id = ?", (event_id,)
        ).fetchone():
            return {"status": "duplicate", "event_id": event_id, "type": event_type}
        status = "recorded"
        if event_type == "checkout.session.completed":
            existing = conn.execute(
                """SELECT payment_intent FROM finance_payments
                   WHERE payment_intent = ? OR session_id = ?""",
                (payment_intent, session_id),
            ).fetchone()
            if existing:
                status = "duplicate"
            else:
                conn.execute(
                    """INSERT INTO finance_payments
                       (payment_intent, session_id, business_id, amount_cents,
                        buyer_hash, event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (payment_intent, session_id, business_id, amount, buyer_hash, event_id, received_at),
                )
        else:
            previous = conn.execute(
                "SELECT * FROM finance_refunds WHERE charge_id = ?", (charge_id,)
            ).fetchone()
            if previous and (
                previous["payment_intent"] != payment_intent
                or previous["charge_amount_cents"] != charge_amount
            ):
                raise ValueError("Identidade ou valor da cobrança alterado no reembolso.")
            if previous and refunded <= previous["amount_refunded_cents"]:
                status = "duplicate"
            else:
                conn.execute(
                    """INSERT INTO finance_refunds
                       (charge_id, payment_intent, amount_refunded_cents,
                        charge_amount_cents, event_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(charge_id) DO UPDATE SET
                         amount_refunded_cents = excluded.amount_refunded_cents,
                         event_id = excluded.event_id, created_at = excluded.created_at""",
                    (charge_id, payment_intent, refunded, charge_amount, event_id, received_at),
                )
        conn.execute(
            """INSERT INTO finance_stripe_events
               (event_id, event_type, status, payment_intent, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, event_type, status, payment_intent, received_at),
        )
    return {
        "status": status,
        "event_id": event_id,
        "type": event_type,
        "payment_intent": payment_intent,
    }


def list_ledger(conn: sqlite3.Connection, business_id: str | None = None) -> list[dict]:
    """List expenses and verified Stripe revenue/refunds without buyer PII.

    Amounts are positive; ``kind`` specifies expense, revenue or refund.
    Unmatched refunds stay pending internally until their sale is received.
    """
    if business_id is not None:
        business_id = _identifier(business_id, "business_id")
    query = "SELECT * FROM finance_expenses"
    args = ()
    if business_id is not None:
        query += " WHERE business_id = ?"
        args = (business_id,)
    ledger = []
    for row in conn.execute(query, args):
        ledger.append({
            "kind": "expense", "business_id": row["business_id"],
            "amount_cents": row["amount_cents"], "description": row["description"],
            "source": "manual", "reference": str(row["id"]),
            "month": row["month"], "created_at": row["created_at"],
        })
    for row in _payment_rows(conn, business_id):
        ledger.append({
            "kind": "revenue", "business_id": row["business_id"],
            "amount_cents": row["amount_cents"], "description": "Venda Stripe verificada",
            "source": "stripe", "reference": row["payment_intent"],
            "month": row["created_at"][:7], "created_at": row["created_at"],
        })
        remaining = row["refund_cents"]
        for refund in conn.execute(
            "SELECT * FROM finance_refunds WHERE payment_intent = ? ORDER BY charge_id",
            (row["payment_intent"],),
        ):
            amount = min(remaining, refund["amount_refunded_cents"])
            if amount:
                ledger.append({
                    "kind": "refund", "business_id": row["business_id"],
                    "amount_cents": amount, "description": "Reembolso Stripe verificado",
                    "source": "stripe", "reference": refund["charge_id"],
                    "month": refund["created_at"][:7], "created_at": refund["created_at"],
                })
                remaining -= amount
    return sorted(ledger, key=lambda entry: (entry["created_at"], entry["kind"], entry["reference"]))
