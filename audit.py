"""
Capillary Product Analyst assignment - data quality audit + Section 2 evidence.
Run:  python audit.py
Expects members.csv and transactions.csv in the same folder.

Every number printed here is what the assignment answers should cite.
Read each section, decide what YOU would do about it, and write that up.
"""
import pandas as pd
import numpy as np

AS_OF = pd.Timestamp("2026-06-30")   # max transaction date in the data
TODAY = pd.Timestamp("2026-08-14")

m = pd.read_csv("members.csv")
t = pd.read_csv("transactions.csv")

print("=" * 70)
print("PART 1 - DATA QUALITY AUDIT")
print("=" * 70)

# ---------- members ----------
print(f"\nmembers.csv: {m.shape[0]:,} rows")

dup_ids = m[m.member_id.duplicated(keep=False)].sort_values("member_id")
print(f"  duplicate member_id rows      : {len(dup_ids)} ({m.member_id.duplicated().sum()} ids)")
print(f"  ...of which EXACT duplicates  : {m.duplicated().sum()}")
# the point: they are NOT exact - attributes conflict, and there is no updated_at
conflict_cols = [c for c in ["tier", "status", "brand", "country"]
                 if dup_ids.groupby("member_id")[c].nunique().max() > 1]
print(f"  conflicting columns in dupes  : {conflict_cols}")
print("  sample conflicting pair:")
first_id = dup_ids.member_id.iloc[0]
print(dup_ids[dup_ids.member_id == first_id][
    ["member_id", "tier", "status", "join_date"]].to_string(index=False))

print(f"\n  tier variants                 : {m.tier.nunique()} distinct")
print(m.tier.value_counts().to_string())

print(f"\n  country variants              : {m.country.nunique()} distinct")
print(m.country.value_counts().to_string())

jd = pd.to_datetime(m.join_date, errors="coerce")
print(f"\n  null join_date                : {m.join_date.isna().sum()}")
print(f"  join_date in the future       : {(jd > TODAY).sum()}  (max {jd.max().date()})")
print(f"  duplicate emails              : {m.email.duplicated().sum()}")

# ---------- transactions ----------
print(f"\ntransactions.csv: {t.shape[0]:,} rows")
print(f"  duplicate transaction_id      : {t.transaction_id.duplicated().sum()}")
print(f"  ...fully identical rows       : {t.duplicated().sum()}")
print(f"  null points_earned            : {t.points_earned.isna().sum()}")
print(f"  transaction_type distinct     : {t.transaction_type.unique().tolist()}"
      "   <- redemptions are NOT separate events")

SENTINEL = 999999.99
print(f"\n  amount == {SENTINEL}          : {(t.amount == SENTINEL).sum()}"
      f"  (brands: {t[t.amount == SENTINEL].brand.unique().tolist()})")
# The full anomalous batch (found via feature-table validation): TO-prefixed ids
to_batch = t[t.transaction_id.str.startswith("TO")]
print(f"  TO-prefixed anomalous batch   : {len(to_batch)} rows, "
      f"all {to_batch.transaction_date.nunique()} timestamp(s), "
      f"amounts {to_batch.amount.min():,.0f}-{to_batch.amount.max():,.2f}")
print(f"  max LEGITIMATE amount         : "
      f"{t[~t.transaction_id.str.startswith('TO')].amount.max():,.2f}"
      "   -> range rule: amount > 1,000 is treated as unknown")
neg = t[t.amount < 0]
print(f"  negative amounts              : {len(neg)}  (min {t.amount.min()})")
print(f"  ...still earning points       : {(neg.points_earned > 0).sum()}"
      f"  totalling {neg.points_earned.sum():,.0f} points")

iso = pd.to_datetime(t.transaction_date, errors="coerce")
print(f"\n  dates failing ISO parse       : {iso.isna().sum()}  (US MM/DD/YYYY format)")
print(f"  sample                        : {t.loc[iso.isna(), 'transaction_date'].head(3).tolist()}")
print("  NOTE: dates like 02/01/2024 are genuinely ambiguous (Feb 1 vs Jan 2).")

orphan = t[~t.member_id.isin(m.member_id)]
print(f"\n  txns with no member record    : {len(orphan):,} rows, "
      f"{orphan.member_id.nunique()} distinct ids")
print(f"  sample orphan ids             : {orphan.member_id.drop_duplicates().head(3).tolist()}"
      "   <- different ID scheme = second source system")
print(f"  points held by orphans        : {orphan.points_earned.sum():,.0f}")

# ---------- cleaned frame used everywhere below ----------
tc = t.drop_duplicates(subset="transaction_id").copy()
tc = tc[~tc.transaction_id.str.startswith('TO')]  # drop anomalous batch (range rule equivalent)
tc["dt"] = pd.to_datetime(tc.transaction_date, format="mixed", errors="coerce")

md = m.drop_duplicates(subset="member_id")[["member_id", "join_date"]]
chk = tc.merge(md, on="member_id", how="left")
chk["jd"] = pd.to_datetime(chk.join_date, errors="coerce")
print(f"\n  txns dated before join_date   : {(chk.dt < chk.jd).sum()}")

by_member = tc.groupby("member_id")[["points_earned", "points_redeemed"]].sum()
print(f"  members redeeming > earned    : {(by_member.points_redeemed > by_member.points_earned).sum()}")

# ======================================================================
print("\n" + "=" * 70)
print("PART 6 - WHICH BRAND IS ACTUALLY MOST GENEROUS")
print("=" * 70)

raw = t.groupby("brand").apply(
    lambda g: g.points_earned.sum() / g.amount.sum(), include_groups=False)
print("\nRatio-of-sums on RAW data (what a naive/marketing calc gives):")
print(raw.round(4).to_string())
print("  ^ PulseMart looks WORST - entirely due to 6 sentinel rows in its denominator.")

pos = tc[(tc.amount > 0) & (tc.amount <= 1000) & tc.points_earned.notna()].copy()
pos["ppd"] = pos.points_earned / pos.amount
print("\nAfter dedup + sentinel removal + positive amounts only:")
clean = pos.groupby("brand").agg(
    n=("ppd", "size"),
    median_ppd=("ppd", "median"),
    mean_ppd=("ppd", "mean"),
    std_ppd=("ppd", "std"),
    ratio_of_sums=("ppd", lambda s: np.nan),
)
clean["ratio_of_sums"] = pos.groupby("brand").apply(
    lambda g: g.points_earned.sum() / g.amount.sum(), include_groups=False)
print(clean.round(4).to_string())
print("\n  Accrual rate is a fixed per-brand constant (std ~0.01-0.02).")

# is the rate confounded by country / currency?
mc = m.drop_duplicates("member_id")[["member_id", "country"]]
pc = pos.merge(mc, on="member_id", how="left")
print("\nMedian ppd by brand x country (checking for a currency confound):")
print(pc.pivot_table(index="brand", columns="country",
                     values="ppd", aggfunc="median").round(3).to_string())
print("  Flat across every country -> no currency effect. Rate is genuinely fixed.")
print("\nCONCLUSION: marketing is wrong. PulseEats is the LEAST generous (1.75).")
print("CAVEAT   : points/dollar only measures generosity if a point is worth the")
print("           same in every brand. No redemption-value data exists here to check.")

# ======================================================================
print("\n" + "=" * 70)
print("PART 8 - OUTSTANDING POINTS LIABILITY (inputs, not the answer)")
print("=" * 70)

earned = pos.points_earned.sum()
earned_all = tc.points_earned.sum()          # includes negative-amount rows
redeemed = tc.points_redeemed.sum()
print(f"\n  points earned (all cleaned rows)      : {earned_all:>15,.0f}")
print(f"  points earned (positive amounts only) : {earned:>15,.0f}")
print(f"  points redeemed                       : {redeemed:>15,.0f}")
print(f"  naive net outstanding                 : {earned_all - redeemed:>15,.0f}")

print("\n  Adjustments you must decide on and justify:")
print(f"    points accrued on refunded txns     : {neg.points_earned.sum():>15,.0f}")
print(f"    points held by orphan members       : {orphan.points_earned.sum():>15,.0f}")
print(f"    rows with null points_earned        : {tc.points_earned.isna().sum():>15,.0f}")

last_txn = tc.groupby("member_id").dt.max()
for months in (12, 24):
    cut = AS_OF - pd.DateOffset(months=months)
    dormant = last_txn[last_txn < cut].index
    pts = tc[tc.member_id.isin(dormant)].points_earned.sum() - \
          tc[tc.member_id.isin(dormant)].points_redeemed.sum()
    print(f"    net points, dormant {months}mo+ ({len(dormant):>5} members): {pts:>12,.0f}")

print("\n  NO points-to-currency rate exists anywhere in the data.")
print("  You must assume one (state it, cite a source, and show sensitivity).")

# ======================================================================
print("\n" + "=" * 70)
print("PART 7 - WIN-BACK CANDIDATE POOL")
print("=" * 70)

p = tc[tc.amount > 0]
agg = p.groupby("member_id").agg(
    last=("dt", "max"), n=("transaction_id", "count"),
    spend=("amount", "sum"), pts=("points_earned", "sum"),
    redeemed=("points_redeemed", "sum"))
agg["recency_days"] = (AS_OF - agg["last"]).dt.days
agg["aov"] = agg.spend / agg.n

print("\nRecency distribution (days since last purchase, as of 2026-06-30):")
print(agg.recency_days.describe([.25, .5, .75, .9]).round(1).to_string())

status = m.drop_duplicates("member_id").set_index("member_id").status
agg = agg.join(status)

q75 = agg.spend.quantile(.75)
pool = agg[(agg.recency_days.between(90, 270)) & (agg.spend >= q75) & (agg.n >= 3)]
print(f"\nPool: lapsed 90-270d, spend >= p75 (${q75:,.2f}), 3+ txns -> {len(pool):,} members")
print("Narrow this to ~20 with YOUR OWN criteria and defend them.")
print("Things worth deciding: exclude status=='churned'? weight by AOV or total spend?")
print("exclude members who never redeemed (no engagement with the programme)?")
print("exclude orphan ids (you have no contact details for them)?")

print("\nDone. Numbers above are the evidence base - the reasoning is yours to write.")
