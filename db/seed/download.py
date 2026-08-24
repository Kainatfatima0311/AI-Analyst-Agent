"""Resolve the dataset into ``db/seed/raw/`` from whichever source is available.

Three sources, tried in this order under ``--source auto``:

1. ``local``     — CSVs (or the Kaggle zip) are already sitting in ``db/seed/raw/``.
2. ``kaggle``    — download ``olistbr/brazilian-ecommerce`` using ``KAGGLE_USERNAME`` and
                   ``KAGGLE_KEY`` from the environment. No extra dependency: the Kaggle
                   public API is a plain authenticated HTTPS download.
3. ``synthetic`` — the deterministic generator in ``generate.py``.

The point of the fallback chain is that the stack never depends on one developer's machine or
on a network download: CI and a first-time contributor both get a working database, while
anyone with Kaggle credentials gets the real dataset with the same command.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

RAW_DIR = Path(__file__).resolve().parent / "raw"

KAGGLE_DATASET = "olistbr/brazilian-ecommerce"
KAGGLE_URL = f"https://www.kaggle.com/api/v1/datasets/download/{KAGGLE_DATASET}"

# The files load.py expects. customer_contact.csv is optional: the real Olist archive has no
# direct identifiers, so only the synthetic source produces it.
REQUIRED_FILES = [
    "olist_customers_dataset.csv",
    "olist_sellers_dataset.csv",
    "olist_products_dataset.csv",
    "product_category_name_translation.csv",
    "olist_geolocation_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
]
OPTIONAL_FILES = ["customer_contact.csv"]


def missing_files(raw_dir: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (raw_dir / name).is_file()]


def extract_any_zip(raw_dir: Path) -> bool:
    """Unpack the first zip found in ``raw_dir``. Returns True if anything was extracted."""
    zips = sorted(raw_dir.glob("*.zip"))
    if not zips:
        return False
    for archive in zips:
        print(f"[download] extracting {archive.name}")
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if member.endswith(".csv"):
                    # Flatten: some archives nest the CSVs in a directory.
                    target = raw_dir / Path(member).name
                    with zf.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    return True


def try_local(raw_dir: Path) -> bool:
    raw_dir.mkdir(parents=True, exist_ok=True)
    if not missing_files(raw_dir):
        print(f"[download] source=local — all {len(REQUIRED_FILES)} required CSVs already present")
        return True
    if extract_any_zip(raw_dir) and not missing_files(raw_dir):
        print("[download] source=local — extracted from a zip already in raw/")
        return True
    return False


def try_kaggle(raw_dir: Path) -> bool:
    username = os.environ.get("KAGGLE_USERNAME")
    key = os.environ.get("KAGGLE_KEY")
    if not (username and key):
        print("[download] kaggle skipped — KAGGLE_USERNAME / KAGGLE_KEY not set")
        return False

    raw_dir.mkdir(parents=True, exist_ok=True)
    archive = raw_dir / "brazilian-ecommerce.zip"
    token = base64.b64encode(f"{username}:{key}".encode()).decode()
    request = urllib.request.Request(KAGGLE_URL, headers={"Authorization": f"Basic {token}"})

    try:
        print(f"[download] fetching {KAGGLE_DATASET} from Kaggle")
        with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as fh:
            shutil.copyfileobj(response, fh)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"[download] kaggle download failed: {exc}")
        archive.unlink(missing_ok=True)
        return False

    extract_any_zip(raw_dir)
    still_missing = missing_files(raw_dir)
    if still_missing:
        print(f"[download] kaggle archive did not contain: {', '.join(still_missing)}")
        return False

    print("[download] source=kaggle — real Olist dataset in place")
    print(
        "[download] note: the real archive has no direct identifiers, so "
        "analytics.customer_contact stays empty. The column policy is still exercised, "
        "because it operates on columns rather than rows."
    )
    (raw_dir / "_manifest.json").write_text(
        json.dumps({"source": "kaggle", "dataset": KAGGLE_DATASET}, indent=2), encoding="utf-8"
    )
    return True


def try_synthetic(raw_dir: Path) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generate import generate

    print("[download] source=synthetic — generating a deterministic Olist-shaped dataset")
    manifest = generate(raw_dir)
    total = sum(manifest["row_counts"].values())
    print(f"[download] generated {total:,} rows across {len(manifest['row_counts'])} files")
    print(f"[download] planted shock month: {manifest['ground_truth']['shock_month']}")
    return True


def resolve(source: str, raw_dir: Path = RAW_DIR) -> str:
    """Ensure the raw CSVs exist. Returns the source actually used."""
    attempts = {
        "auto": [("local", try_local), ("kaggle", try_kaggle), ("synthetic", try_synthetic)],
        "local": [("local", try_local)],
        "kaggle": [("kaggle", try_kaggle)],
        "synthetic": [("synthetic", try_synthetic)],
    }[source]

    for name, fn in attempts:
        if fn(raw_dir):
            return name

    raise SystemExit(
        f"[download] could not resolve the dataset with source={source}.\n"
        f"  Missing: {', '.join(missing_files(raw_dir)) or 'nothing'}\n"
        f"  Either place the Olist CSVs in {raw_dir}, set KAGGLE_USERNAME/KAGGLE_KEY,\n"
        f"  or run with --source synthetic."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=["auto", "local", "kaggle", "synthetic"],
        default="auto",
        help="where to get the dataset from (default: auto — local, then kaggle, then synthetic)",
    )
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    args = parser.parse_args()

    used = resolve(args.source, args.raw_dir)
    present = [f for f in REQUIRED_FILES + OPTIONAL_FILES if (args.raw_dir / f).is_file()]
    print(f"[download] done — source={used}, {len(present)} files in {args.raw_dir}")


if __name__ == "__main__":
    main()
