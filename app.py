"""Local page: repeating payees from a bank CSV, and cash projected forward."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from recurring import (
    group_payees,
    historical_day_end,
    latest_balance,
    load_transactions,
    project_cash,
    project_cash_daily,
)

DEFAULT_CSV = Path.home() / "Downloads" / "ExportedTransactions.csv"
MONEY = "$%.2f"


def main() -> None:
    st.set_page_config(page_title="Recurring cashflow", layout="wide")
    st.title("Recurring cashflow")
    st.caption("Runs on this computer. The CSV is not sent anywhere.")

    with st.sidebar:
        st.header("Source")
        uploaded = st.file_uploader("Bank CSV", type=["csv"])
        path = st.text_input(
            "Or a file path",
            value=str(DEFAULT_CSV) if DEFAULT_CSV.exists() else "",
        )
        st.caption("A chosen file is used instead of the path.")
        min_months = st.slider("Minimum months with activity", min_value=2, max_value=6, value=3)
        active_within = st.slider(
            "Drop if inactive for (months)",
            min_value=1,
            max_value=12,
            value=2,
            help="A payee with no charge in this many calendar months (relative to the newest row in the CSV) is not treated as recurring.",
        )
        horizon = st.slider("Projection horizon (months)", min_value=3, max_value=60, value=12)

    source = uploaded if uploaded is not None else path.strip()
    if not source:
        st.info("Choose a bank CSV to list repeating payees and project the balance.")
        return
    if uploaded is None and not Path(path.strip()).is_file():
        st.error(f"No file at {path.strip()}")
        return

    try:
        transactions = load_transactions(source)
    except (OSError, ValueError, UnicodeError) as exc:
        st.error(str(exc))
        return

    groups = group_payees(
        transactions,
        min_months=min_months,
        active_within_months=active_within,
    )
    categories = sorted({group.category for group in groups if group.category})
    with st.sidebar:
        exclude = st.multiselect("Exclude categories", categories)

    excluded = set(exclude)
    visible = [group for group in groups if group.category not in excluded]
    expenses = sorted(
        (group for group in visible if group.direction == "expense"),
        key=lambda group: (group.monthly_avg, group.payee),
    )
    incomes = sorted(
        (group for group in visible if group.direction == "income"),
        key=lambda group: (-group.monthly_avg, group.payee),
    )

    # Containers are filled later so the figures stay above the editable tables
    # that supply the include checkboxes.
    metrics_slot = st.container()
    chart_slot = st.container()
    table_slot = st.container()

    filter_key = f"{min_months}|{active_within}|{'|'.join(exclude)}"
    with table_slot:
        st.subheader("Recurring expenses")
        expense_table = _editor(expenses, f"expenses-{filter_key}|{_payee_key(expenses)}")
        st.subheader("Recurring income")
        income_table = _editor(incomes, f"income-{filter_key}|{_payee_key(incomes)}")

    income = _included_sum(income_table)
    outflows = _included_sum(expense_table)
    as_of, balance = latest_balance(transactions)
    projection = project_cash(balance, as_of, horizon, income, outflows)
    ending = projection[-1].ending_balance
    first = min(item.posting_date for item in transactions)
    last = max(item.posting_date for item in transactions)

    with metrics_slot:
        st.caption(f"Starting from {_money(balance)} posted on {_long_date(as_of)}.")
        one, two, three, four = st.columns(4)
        one.metric("Monthly income", _money(income))
        two.metric("Monthly outflows", _money(outflows))
        three.metric("Monthly net", _money(income + outflows))
        four.metric(f"Balance in {horizon} months", _money(ending))

    with chart_slot:
        history_col, future_col = st.columns(2)
        with history_col:
            st.subheader("Posted balance")
            history = historical_day_end(transactions)
            history_frame = pd.DataFrame(
                {
                    "Day": [day.isoformat() for day, _amount in history],
                    "Balance": [amount for _day, amount in history],
                }
            )
            st.line_chart(history_frame, x="Day", y="Balance")
        with future_col:
            st.subheader("Projected balance")
            daily_points = project_cash_daily(balance, as_of, horizon, income, outflows)
            future_frame = pd.DataFrame(
                {
                    "Day": [day.isoformat() for day, _amount in daily_points],
                    "Balance": [amount for _day, amount in daily_points],
                }
            )
            st.line_chart(future_frame, x="Day", y="Balance")
        st.caption(
            f"Averages cover {_long_date(first)} through {_long_date(last)}. "
            "Charts use daily points; the first and last months are partial. "
            "This is a flat historical average, not a seasonal forecast."
        )

        included_expenses = _included(expense_table).head(15)
        if not included_expenses.empty:
            st.subheader("Largest monthly expense averages")
            chart_rows = included_expenses.copy()
            chart_rows["Monthly average"] = chart_rows["Monthly average"].abs()
            st.bar_chart(chart_rows, x="Payee", y="Monthly average")

        st.subheader("Month by month")
        future_rows = [row for row in projection if not row.is_start]
        month_frame = pd.DataFrame(
            {
                "Month": [row.label for row in future_rows],
                "Income": [row.income for row in future_rows],
                "Outflows": [row.outflows for row in future_rows],
                "Net": [row.net for row in future_rows],
                "Ending balance": [row.ending_balance for row in future_rows],
            }
        )
        st.dataframe(
            month_frame,
            hide_index=True,
            column_config={
                "Income": st.column_config.NumberColumn(format=MONEY),
                "Outflows": st.column_config.NumberColumn(format=MONEY),
                "Net": st.column_config.NumberColumn(format=MONEY),
                "Ending balance": st.column_config.NumberColumn(format=MONEY),
            },
        )


def _payee_key(groups: list) -> str:
    return ",".join(group.payee for group in groups)


def _editor(groups: list, key: str) -> pd.DataFrame:
    frame = _frame(groups)
    if frame.empty:
        st.caption("Nothing repeats across this many months.")
        return frame
    return st.data_editor(
        frame,
        key=key,
        hide_index=True,
        disabled=[column for column in frame.columns if column != "Include"],
        column_config={
            "Include": st.column_config.CheckboxColumn("Include", default=True),
            "Total": st.column_config.NumberColumn(format=MONEY),
            "Avg ticket": st.column_config.NumberColumn(format=MONEY),
            "Monthly average": st.column_config.NumberColumn(format=MONEY),
        },
    )


def _frame(groups: list) -> pd.DataFrame:
    columns = [
        "Include",
        "Payee",
        "Category",
        "Times",
        "Months",
        "Total",
        "Avg ticket",
        "Monthly average",
        "First",
        "Last",
    ]
    rows = [
        {
            "Include": True,
            "Payee": group.payee,
            "Category": group.category,
            "Times": group.count,
            "Months": group.months,
            "Total": group.total,
            "Avg ticket": group.avg_ticket,
            "Monthly average": group.monthly_avg,
            "First": group.first_date.isoformat(),
            "Last": group.last_date.isoformat(),
        }
        for group in groups
    ]
    return pd.DataFrame(rows, columns=columns)


def _included(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[frame["Include"].astype(bool)]


def _included_sum(frame: pd.DataFrame) -> float:
    picked = _included(frame)
    if picked.empty:
        return 0.0
    return float(picked["Monthly average"].sum())


def _long_date(day: date) -> str:
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


if __name__ == "__main__":
    main()
