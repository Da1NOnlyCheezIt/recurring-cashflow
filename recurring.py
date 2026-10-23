"""Detect repeating payees in a bank CSV and project cash forward.

The monthly rate for a payee is its total divided by the full span of the
export, not by the months that payee happened to appear. A charge that shows
up a few times is not treated as a bill of that size every month.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

DAYS_PER_MONTH = 30.4375

REQUIRED_COLUMNS = ("Posting Date", "Amount", "Description", "Balance")

# Longer phrases first so "debit card purchase" is not left as "purchase".
CHANNEL_PHRASES = (
    "ONLINE BANKING DEPOSIT",
    "ONLINE BANKING TRANSFER",
    "ONLINE BANKING",
    "DEBIT CARD PURCHASE",
    "DEBIT CARD",
    "ACH WITHDRAWAL",
    "ACH DEPOSIT",
    "PAYMENT TO",
)

NOISE_SUFFIXES = frozenset({"MARKETPLACE", "MARKET", "STORE", "STORES", "ONLINE"})

# Receipt debris that is not part of the payee name. Dropped only when a real
# name token remains, so a payee that is just one of these words still shows.
JUNK_TOKENS = frozenset({"AMZN", "COM", "WWW", "HTTP", "HTTPS", "MKTPL"})

STATE_CODES = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI",
        "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
        "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
        "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA",
        "WI", "WV", "WY",
    }
)

COMPANY_RE = re.compile(r"COMPANY:\s*(.*?)\s*ENTRY:", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Transaction:
    source_index: int
    posting_date: date
    amount: float
    description: str
    extended_description: str
    category: str
    balance: float | None


@dataclass(frozen=True)
class PayeeGroup:
    payee: str
    direction: str
    category: str
    count: int
    months: int
    total: float
    avg_ticket: float
    monthly_avg: float
    first_date: date
    last_date: date

    @property
    def key(self) -> str:
        return f"{self.direction}|{self.payee}"


@dataclass(frozen=True)
class MonthProjection:
    label: str
    income: float
    outflows: float
    net: float
    ending_balance: float
    is_start: bool = False


def load_transactions(source: str | Path | TextIO | io.BytesIO) -> list[Transaction]:
    """Read a bank export. `source` is a path or an open text or binary file."""
    if isinstance(source, (str, Path)):
        with open(source, newline="", encoding="utf-8-sig") as handle:
            return _parse(handle)
    raw = source.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    return _parse(io.StringIO(raw))


def _parse(handle: TextIO) -> list[Transaction]:
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")
    missing = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
    if missing:
        raise ValueError("CSV is missing columns: " + ", ".join(missing))

    transactions: list[Transaction] = []
    for index, row in enumerate(reader):
        date_raw = (row.get("Posting Date") or "").strip()
        amount_raw = (row.get("Amount") or "").strip().replace(",", "")
        if not date_raw or not amount_raw:
            continue
        try:
            posting_date = datetime.strptime(date_raw, "%m/%d/%Y").date()
            amount = float(amount_raw)
        except ValueError:
            continue
        balance = _parse_balance(row.get("Balance"))
        transactions.append(
            Transaction(
                source_index=index,
                posting_date=posting_date,
                amount=amount,
                description=(row.get("Description") or "").strip(),
                extended_description=(row.get("Extended Description") or "").strip(),
                category=(row.get("Transaction Category") or "").strip(),
                balance=balance,
            )
        )
    if not transactions:
        raise ValueError("CSV has no usable transactions.")
    return transactions


def _parse_balance(raw: str | None) -> float | None:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_payee(description: str, extended: str = "") -> str:
    """Collapse a bank description to a short payee key."""
    raw = (description or "").strip() or (extended or "").strip()
    if not raw:
        return "UNKNOWN"
    company = COMPANY_RE.search(raw)
    if company:
        return _clean_tokens(company.group(1))
    upper = raw.upper()
    for phrase in CHANNEL_PHRASES:
        if upper.startswith(phrase):
            upper = upper[len(phrase) :].lstrip(" :-")
            break
    return _clean_tokens(upper)


def _clean_tokens(text: str) -> str:
    # Apostrophes are removed first so "Lowe's" does not split into two tokens.
    upper = text.upper().replace("'", "").replace("’", "")
    cleaned = re.sub(r"[^A-Z0-9*]+", " ", upper)
    tokens: list[str] = []
    for index, token in enumerate(cleaned.split()):
        pieces = token.split("*") if "*" in token else [token]
        for part in pieces:
            if not part or set(part) == {"X"}:
                continue
            if part.isdigit():
                # Keep a leading brand number such as "365". Drop other numbers.
                if index == 0 and not tokens:
                    tokens.append(part)
                continue
            if not part.isalpha():
                continue
            tokens.append(part)
    if any(token not in JUNK_TOKENS for token in tokens):
        tokens = [token for token in tokens if token not in JUNK_TOKENS]
    if len(tokens) >= 3 and tokens[-1] in STATE_CODES:
        tokens = tokens[:-2]
    elif len(tokens) >= 2 and tokens[-1] in STATE_CODES:
        tokens = tokens[:-1]
    while tokens and tokens[-1] in NOISE_SUFFIXES:
        tokens.pop()
    if len(tokens) >= 2 and len(tokens[-1]) == 1 and len(tokens[-2]) >= 4:
        tokens.pop()
    if not tokens:
        return "UNKNOWN"
    return " ".join(tokens[:4])


def window_months(transactions: list[Transaction]) -> float:
    if not transactions:
        raise ValueError("No transactions.")
    dates = [item.posting_date for item in transactions]
    span_days = (max(dates) - min(dates)).days
    return max(span_days, 1) / DAYS_PER_MONTH


def latest_balance(transactions: list[Transaction]) -> tuple[date, float]:
    """Balance on the newest posting date.

    When several rows share that date, the earliest row in the file wins.
    Bank exports list the newest transaction first.
    """
    best: Transaction | None = None
    for item in transactions:
        if item.balance is None:
            continue
        if best is None or (item.posting_date, -item.source_index) > (
            best.posting_date,
            -best.source_index,
        ):
            best = item
    if best is None or best.balance is None:
        raise ValueError("CSV has no balance.")
    return best.posting_date, best.balance


def historical_month_end(transactions: list[Transaction]) -> list[tuple[date, float]]:
    """Last posted balance in each calendar month, oldest month first."""
    best: dict[tuple[int, int], Transaction] = {}
    for item in transactions:
        if item.balance is None:
            continue
        key = (item.posting_date.year, item.posting_date.month)
        current = best.get(key)
        rank = (item.posting_date, -item.source_index)
        if current is None or rank > (current.posting_date, -current.source_index):
            best[key] = item
    ordered = sorted(best.values(), key=lambda item: item.posting_date)
    return [(item.posting_date, item.balance) for item in ordered if item.balance is not None]


def historical_day_end(transactions: list[Transaction]) -> list[tuple[date, float]]:
    """Last posted balance on each calendar day that has a balance, oldest first."""
    best: dict[date, Transaction] = {}
    for item in transactions:
        if item.balance is None:
            continue
        key = item.posting_date
        current = best.get(key)
        if current is None or -item.source_index > -current.source_index:
            best[key] = item
    ordered = sorted(best.values(), key=lambda item: item.posting_date)
    return [(item.posting_date, item.balance) for item in ordered if item.balance is not None]


def group_payees(
    transactions: list[Transaction],
    min_months: int = 3,
    active_within_months: int = 2,
) -> list[PayeeGroup]:
    """Group by payee and sign.

    Keep groups that appear in `min_months` distinct months and whose most
    recent charge falls within `active_within_months` calendar months of the
    newest transaction in the export. A payee that stopped earlier is dropped.
    """
    span = window_months(transactions)
    as_of = max(item.posting_date for item in transactions)
    activity_cutoff = _shift_month(as_of, -active_within_months)
    buckets: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for item in transactions:
        if item.amount == 0:
            continue
        direction = "income" if item.amount > 0 else "expense"
        payee = normalize_payee(item.description, item.extended_description)
        buckets[(direction, payee)].append(item)

    groups: list[PayeeGroup] = []
    for (direction, payee), items in buckets.items():
        month_keys = {(item.posting_date.year, item.posting_date.month) for item in items}
        if len(month_keys) < min_months:
            continue
        dates = [item.posting_date for item in items]
        last_date = max(dates)
        if (last_date.year, last_date.month) < (activity_cutoff.year, activity_cutoff.month):
            continue
        total = sum(item.amount for item in items)
        categories = Counter(item.category or "Uncategorized" for item in items)
        groups.append(
            PayeeGroup(
                payee=payee,
                direction=direction,
                category=categories.most_common(1)[0][0],
                count=len(items),
                months=len(month_keys),
                total=total,
                avg_ticket=total / len(items),
                monthly_avg=total / span,
                first_date=min(dates),
                last_date=last_date,
            )
        )
    return groups


def project_cash(
    starting_balance: float,
    as_of: date,
    horizon: int,
    income: float,
    outflows: float,
) -> list[MonthProjection]:
    """Start at the latest balance. Each later month adds income + outflows once.

    `outflows` is negative. The first row is the posted balance, labeled Now.
    Later rows are calendar months after `as_of`.
    """
    net = income + outflows
    rows = [
        MonthProjection(
            label="Now",
            income=0.0,
            outflows=0.0,
            net=0.0,
            ending_balance=starting_balance,
            is_start=True,
        )
    ]
    for step in range(1, horizon + 1):
        month = _shift_month(as_of, step)
        rows.append(
            MonthProjection(
                label=f"{month.year:04d}-{month.month:02d}",
                income=income,
                outflows=outflows,
                net=net,
                ending_balance=starting_balance + net * step,
            )
        )
    return rows


def project_cash_daily(
    starting_balance: float,
    as_of: date,
    horizon_months: int,
    income: float,
    outflows: float,
) -> list[tuple[date, float]]:
    """Daily balance path for charts. Spreads the monthly net evenly; end matches monthly horizon."""
    net = income + outflows
    horizon_days = max(int(round(horizon_months * DAYS_PER_MONTH)), 1)
    points: list[tuple[date, float]] = [(as_of, starting_balance)]
    for step in range(1, horizon_days + 1):
        months_elapsed = horizon_months * (step / horizon_days)
        points.append(
            (as_of + timedelta(days=step), starting_balance + net * months_elapsed)
        )
    return points


def _shift_month(day: date, steps: int) -> date:
    """Move `day` by `steps` calendar months. Day-of-month is clamped to 1."""
    index = day.year * 12 + (day.month - 1) + steps
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)
