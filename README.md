# Recurring cashflow

A local page that reads a bank CSV, lists payees that repeat across several months, and projects the checking balance from the latest posted balance.

The file stays on this computer. The app does not send it anywhere.

## Run

```bash
cd ~/Projects/recurring-cashflow
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Streamlit opens a browser tab. If `~/Downloads/ExportedTransactions.csv` is present, that path is filled in. Choosing a file in the sidebar overrides the path.

Uncheck a row to drop it from the projection. Exclude a category, such as Transfers or Securities Trade, to drop every repeat in that category.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

The tests use made-up rows. They do not read a real export.

## How the numbers work

A payee counts when it shows up in at least three different calendar months. You can move that cutoff from 2 to 6. Money in and money out are separate, even when the description matches.

The monthly average divides the payee's total by the full span of the file:

```text
monthly average = total / (days from first transaction to last / 30.4375)
```

A paycheck every two weeks and a once-a-month bill land on the same scale. A payee that only appears a few times is not projected as if that whole charge happens every month. The table still shows the average charge and how many months it actually appeared.

The projection starts at the newest posted balance and adds the monthly net once per future month. It is a flat average, not a seasonal forecast. The first and last months in an export are often partial.
