"""
Runner for the Part 2 feature table.

Usage:
    python run_features.py

Expects in the same folder:
    members.csv, transactions.csv, features.sql

Produces:
    assignment.db        (SQLite - raw + prepped tables)
    feature_table.csv    (the Part 2 deliverable)

Prep happens in Python (date normalisation, member dedup + flags),
feature logic lives in features.sql. Only stdlib + pandas needed.
"""
import sqlite3
import pandas as pd

DB = "assignment.db"
SNAPSHOT = "2026-06-30"   # max transaction date in the data

con = sqlite3.connect(DB)

# ---------- prep layer (Part 1 decisions that must precede SQL) ----------

# transactions: normalise mixed date formats (ISO + MM/DD/YYYY) so
# SQLite date functions work. NOTE: rows like 02/01/2024 are ambiguous;
# pandas 'mixed' reads them as US-style MM/DD. Documented as an assumption.
t = pd.read_csv("transactions.csv")
t["txn_date"] = pd.to_datetime(t.transaction_date, format="mixed").dt.strftime("%Y-%m-%d")
t[["transaction_id", "member_id", "txn_date", "amount",
   "points_earned", "points_redeemed", "channel", "brand"]].to_sql(
    "transactions_clean", con, index=False, if_exists="replace")

# members: trim/title-case tier variants, dedup on member_id,
# flag the 150 ids whose duplicate rows conflicted (tier gets nulled
# in the SQL for these), flag missing join_dates.
m = pd.read_csv("members.csv")
m["tier"] = m.tier.str.strip().str.title()
dup_ids = m.member_id[m.member_id.duplicated()].unique()
md = m.drop_duplicates("member_id").copy()
md["is_tier_ambiguous"] = md.member_id.isin(dup_ids).astype(int)
md["join_date_missing"] = md.join_date.isna().astype(int)
md.to_sql("members_dedup", con, index=False, if_exists="replace")

print(f"prep: {len(t):,} txns loaded, {len(md):,} members "
      f"({len(dup_ids)} flagged tier-ambiguous)")

# ---------- run the feature SQL ----------
with open("features.sql") as fh:
    sql = fh.read()

features = pd.read_sql(sql, con)
features.to_csv("feature_table.csv", index=False)

# ---------- sanity checks (never ship numbers you haven't looked at) ----------
print(f"\nfeature table: {features.shape[0]:,} members x {features.shape[1]} cols")
assert features.member_id.is_unique, "duplicate members in output!"
assert features[features.is_tier_ambiguous == 1].tier.isna().all(), \
    "ambiguous members should have NULL tier"
print("checks passed: unique member_id, ambiguous tiers nulled")

print("\nkey feature summary:")
print(features[["days_since_last_txn", "txn_count_lifetime", "total_spend",
                "refund_rate", "redemption_rate", "points_balance"]]
      .describe().round(2).loc[["mean", "50%", "min", "max"]].to_string())

print(f"\nnegative adjusted balances: {(features.points_balance < 0).sum()} "
      "(the Part 1 redeemed-more-than-earned anomalies surfacing here)")
print("\nwritten: feature_table.csv")
