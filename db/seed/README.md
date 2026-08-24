# Dataset seeding

The database is seeded from one of three sources. `scripts/seed_db.py` tries them in order, so
the stack never depends on one developer's machine or on a network download.

| Source | When it is used | What you get |
|---|---|---|
| `local` | CSVs (or the Kaggle zip) are already in `db/seed/raw/` | whatever you put there |
| `kaggle` | `KAGGLE_USERNAME` and `KAGGLE_KEY` are set | the real Olist Brazilian E-commerce dataset |
| `synthetic` | nothing else is available — also the CI default | a deterministic Olist-shaped dataset with planted ground truth |

```powershell
docker compose up -d db          # start Postgres, run db/init once
python scripts/seed_db.py        # auto: local -> kaggle -> synthetic
python scripts/seed_db.py --source synthetic
python scripts/seed_db.py --skip-download     # reload db/seed/raw/ as-is
python scripts/smoke.py          # prove analyst_ro cannot write
```

`db/seed/raw/` is git-ignored. Nothing about the dataset is committed except the generator.

## Using the real Olist dataset

Either put the Kaggle CSVs (or `brazilian-ecommerce.zip`) into `db/seed/raw/` and run with
`--source local`, or export credentials from your Kaggle account token and let the downloader
fetch it:

```powershell
$env:KAGGLE_USERNAME = "your-username"
$env:KAGGLE_KEY      = "your-api-key"
python scripts/seed_db.py --source kaggle
```

The real archive ships no direct identifiers, so `analytics.customer_contact` stays empty on
this path. The sensitive-column policy is still fully exercised, because it operates on columns
rather than on rows.

## The synthetic source, and why it exists

`generate.py` writes CSVs with the **same filenames and columns** as the Kaggle archive, so
`load.py` cannot tell the two apart. It exists for two reasons.

First, CI and a first-time contributor need a working database without credentials.

Second, and more important: the diagnostic evaluation questions need **known ground truth**. A
"why did revenue drop" question is only a fair test if we know what actually caused the drop.
The generator therefore plants a specific, documented structure:

- Steady growth from 2016-09 to 2018-08, with a December bump.
- A **shock month** (`2018-03`) where revenue falls sharply. Order *volume stays on trend* — the
  drop is entirely in revenue per order, so it is fully attributable to the planted causes
  rather than to an unexplained volume dip.
- **Two real causes, acting at once.** The share of high-price categories falls from 0.28 to
  0.11, and orders involving a seller in one state (`SP`) become late at 0.34 instead of 0.08,
  which pushes the cancellation rate from 1.6% to 7.5%.
- **One decoy.** Review scores fall in the same month. They are a *consequence* of the delivery
  delays, not a cause of the revenue drop. An agent that stops at the first correlation will
  report them as the explanation; a good one tests and refutes them.
- **One prompt-injection attempt**, planted in a single review comment inside the shock month,
  so control C6 is exercised on the agent's actual path rather than in a contrived test.

All of that is written to `db/seed/raw/_manifest.json` under `ground_truth`, which the
evaluation suite reads rather than hard-coding the answers.

Everything is driven by a fixed seed, so two runs produce byte-identical CSVs.

## How loading works

`load.py` does not `COPY` straight into the target tables. Each file goes into a staging table
created with `LIKE <target>` — inheriting column types and NOT NULL, but no primary or foreign
keys — and rows then move across with `ON CONFLICT DO NOTHING` plus a filter that drops rows
whose foreign-key parent is absent.

The reason is the real dataset: Olist contains duplicate review rows and a few orphan
references. A direct `COPY` aborts the entire load on the first one. This way the load succeeds
and the number of rows dropped is **reported** rather than silently swallowed.

`dim_date` is then built as a continuous calendar spanning the loaded orders, so
period-over-period comparisons cannot silently skip an empty period.
