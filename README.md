# Capillary Product Analyst Assignment — Hrithik N L

All figures in this document are reproducible by running `python audit.py` and
`python run_features.py` with `members.csv` and `transactions.csv` in the same
folder. Files included: `audit.py`, `features.sql`, `run_features.py`,
`feature_table.csv`.

Snapshot date used throughout: **2026-06-30** (max transaction date in the data).

---

## TL;DR — five findings a reviewer should see first

1. **The data contains a planted 15-row batch** (ids TO00000–TO00014, one
   timestamp, amounts to $999,999.99). My initial exact-value check caught 6 of
   15; validating the finished feature table caught the rest — after they had
   already put six fake "$100K whales" at the top of my first win-back list.
   Fixed with a range rule; every downstream number regenerated.
2. **Marketing's claim in Part 6 is exactly inverted:** PulseEats is the *least*
   generous brand (1.75 pts/$ vs PulseMart's 2.30). The raw data says otherwise
   only because the fake batch inflates PulseMart's denominator.
3. **Refunds still earn points** — 2,012 return transactions kept 268,299 points.
   That's a policy bug and a farmable exploit, not a data-cleaning task.
4. **Points liability ≈ $229K** at an assumed 1¢/point (22.94M net points), with
   four named reasons to distrust it before it touches a balance sheet.
5. **Every issue in the audit entered through an unguarded source boundary** —
   which is why the Part 3 design puts contracts and quality gates at ingestion
   rather than cleaning downstream.

---

# SECTION 1 — Pipeline & Modelling Track

## Part 1 — Data Quality & Governance Audit

**members.csv — 50,160 rows**

| Issue | Evidence (how I found it) | Likely root cause | Fix | Prevention control |
|---|---|---|---|---|
| 150 member_ids duplicated with **conflicting tier/status** (300 rows) | `member_id.duplicated()`; rows are not identical — e.g. M00073 is Bronze/inactive in one row, Silver/active in the other. No `updated_at` column exists, so the current version cannot be determined | Two extracts or CDC snapshots unioned without dedup; source lacks an update timestamp | Keep one row per member, null the tier, set `is_tier_ambiguous = 1`, exclude tier features for these 150 (0.3% of members) rather than guess | Require `updated_at` in the source contract; enforce PK uniqueness at ingestion — fail the load, don't pass duplicates through |
| Tier casing/whitespace variants (~800 rows) | `value_counts()` shows ` Bronze`, `GOLD`, `gold`, `platinum`, `Gold ` etc. | Free-text entry / multiple source systems, no enum enforcement | `TRIM` + title-case at ingestion | Enum constraint on tier; reject values outside the domain |
| Country not standardised (~500 rows) | `IN`/`India`/`in`, `US`/`usa`/`U.S.`, `SG`/`Singapore` | No reference table at source | Map to ISO-3166 codes | Reference-table lookup at ingestion; unmapped values quarantine |
| 999 null join_dates | `isna().sum()` | Optional field at signup / migration gaps | Cannot invent a date: flag `join_date_missing`; for tenure features fall back to first transaction date as a documented proxy | NOT NULL constraint on new records |
| 30 future join_dates (max 2027-03-15) | `join_date > load_date` | Fat-finger entry or timezone bug | Null + flag | `join_date <= load_date` check at ingestion |
| 170 duplicate emails | `email.duplicated()` | Households sharing an email, or duplicate accounts | Flag only — legitimate in loyalty (families); do not merge automatically | Uniqueness *warning* (not hard fail) + periodic identity-resolution review |

**transactions.csv — 194,311 rows**

| Issue | Evidence | Likely root cause | Fix | Prevention control |
|---|---|---|---|---|
| 1,995 duplicate transaction_ids — **1,936 fully identical rows, 59 sharing an id with different content** | `transaction_id.duplicated()` vs `df.duplicated()` | Identical rows: a pipeline replay/retry appended rows that already existed (non-idempotent ingestion). The 59 conflicts: id collision or partial update | Dedup identical rows (keep first); quarantine the 59 conflicts — they are conflicts, not duplicates | Idempotent ingestion via MERGE on transaction_id (see Part 3); PK uniqueness at load |
| **A 15-row anomalous batch: transaction_ids TO00000–TO00014** (every other id is T-prefixed), all timestamped 2026-02-15 00:00:00 exactly, amounts from 64,000 to 999999.99, points inconsistent with any brand's accrual rate (~0.0015/dollar vs real rates 1.75–2.30) | Found in two passes. First pass caught 6 rows at exactly 999999.99 (the max of a DECIMAL(8,2) — a classic "unknown" sentinel). Then a validation check on the finished feature table — inspecting members with >$50K lifetime spend — exposed members with four $30–45 purchases and one $99,999 outlier, which unravelled the rest: one injected batch, sequential ids, single timestamp | A test or erroneous batch load from a different process (distinct id prefix = different id-issuing source), never rolled back | Amounts nulled by **range rule** (`amount > 1,000 → NULL`; max legitimate transaction in the data is $600). Rows kept as events. The exact-value check caught only 6 of 15 — the range rule catches the whole class | Three layers at ingestion: range check (this alone catches all 15), id-format validation (TO-prefix fails a `^T\d+$` pattern check), distribution-drift alert vs trailing 30 days. The lesson this batch teaches: enumerate-the-sentinels is the weakest of the three — I initially missed 9 of these 15 rows by relying on it |
| 2,012 negative amounts (refunds) **still earning positive points** — 268,299 points total | `amount < 0` rows all typed `purchase`, all with points_earned > 0 | The loyalty engine grants accrual on purchase and never reverses it on return. **This is a policy bug, not a data bug — the pipeline faithfully recorded what the source wrongly did** | Keep refunds as behavioural signal (`refund_count`, `refund_rate` become churn features); compute points both gross and adjusted (accrual on refunds reversed) and let each downstream consumer pick | Accrual reversal on refund in the loyalty engine; reconciliation check flagging members whose points don't match transaction history. Left as-is, this is a farmable exploit: buy → return → keep points → repeat |
| 3,000 dates in MM/DD/YYYY mixed with ISO | ISO parse fails on exactly these rows; e.g. `05/22/2024`. Dates like `02/01/2024` are genuinely ambiguous (Feb 1 vs Jan 2) | One source (or one source after an update) changed date format mid-stream — format drift nobody detected | Parse with explicit format handling; document the US-format assumption for ambiguous rows | Format-conformance check per column: alert when % of rows matching the expected pattern drops (catches drift the day it starts, not months later) |
| 2,988 member_ids in transactions with **no member record** — all in an `M9000000+` range, holding 388,681 points | Anti-join against members; the id range is a different numbering scheme | A second id-issuing source. Ranked hypotheses: (1) guest/unregistered checkouts assigned synthetic ids at POS — supported by these ids averaging ~1–2 transactions each, mostly one-offs; (2) an acquired/legacy system never migrated; (3) failed sync. Distinguishing test: repeat rate per id | Exclude from the churn feature table (no profile, no contact — unactionable); include in the liability with a separate line item (Part 8) | Referential-integrity check at load: transaction member_ids must exist in the member dimension or route to a holding table |
| 1,514 null points_earned | `isna().sum()` | Source outage or field added later | Treat as unknown, not zero — COALESCE only where the metric definition demands it, and count them | Not-null rate monitor per column |
| 672 transactions dated before the member's join_date | Join to members, `txn_date < join_date` | Timezone mismatch, backdated migration, or the join_date is wrong | Flag; do not delete — the transaction evidence is usually more reliable than a profile field | Cross-field validation rule at load |
| 5 members with lifetime redeemed > earned | Groupby comparison | Missing earn history (pre-export) or a redemption bug | Flag; surfaces again as negative balances in the feature table (Part 2) | Balance reconciliation check |
| 20,308 transactions where txn brand ≠ member's registered brand | Join comparison | Likely legitimate cross-brand shopping within the group | Flag only — do not "fix"; possibly a real behaviour signal | Document as expected behaviour after confirming with the business |

The common thread: **every issue above entered through an unguarded boundary.** The
design in Part 3 treats the source boundary as the enforcement point, so this class
of problem is prevented rather than cleaned.

Additional check performed: inspecting feature-table members with >$50K lifetime
spend is what exposed the remaining 9 rows of the TO batch (above) — the audit
caught 6 of 15 by exact value; validating the *output* caught the rest.

---

## Part 2 — Feature Engineering

Deliverables: `features.sql` (the feature logic), `run_features.py` (prep layer +
runner), `feature_table.csv` (output: **49,996 members × 19 columns**).

Feature set — RFM base plus two additions:

| Group | Features | Why |
|---|---|---|
| Recency | `days_since_last_txn`, `days_since_last_redemption` | Strongest single churn predictor; redemption recency measures engagement with the *program*, not just the store |
| Frequency | `txn_count_lifetime`, `txn_count_last_90d`, `avg_days_between_txns` | Habit strength |
| Monetary | `total_spend`, `avg_order_value`, `refund_count`, `refund_rate` | Value; refund behaviour doubles as a dissatisfaction/fraud signal (Part 1) |
| Trend | `spend_trend_90d` (last 90d ÷ prior 90d) | Recency tells you someone *stopped*; trend tells you someone is *stopping* — the feature that makes intervention possible before the win-back window closes |
| Points | `points_earned_gross`, `points_earned_adj`, `points_redeemed`, `points_balance`, `redemption_rate` | A member sitting on a growing pile of unredeemed points has disengaged from the program even while still shopping. Gross and adjusted both kept per the Part 1 refund decision |
| Flags | `is_tier_ambiguous`, `join_date_missing` | The model knows which members have soft data |

**Part 1 issues handled in this table (3+ required; all carried in, each commented
in the SQL at the point it is enforced):**

1. **Duplicate member_ids** → member kept, tier nulled, `is_tier_ambiguous` flag.
   Chosen over "take the higher tier" (assumes upgrades only — a directional bias)
   and "last row wins" (file order unverifiable). Cost of exclusion: 0.3% of
   members; cost of guessing wrong: contaminating what may feed the churn label.
   Note this exclusion applies **only to the analytical feature table** — no
   operational impact; the member's login and points are untouched.
2. **Anomalous batch amounts** → `CASE WHEN amount > 1000 THEN NULL`, a range
   rule rather than an exact-value list (which missed 9 of the batch's 15 rows on
   the first pass), and not a WHERE filter: the events are real, the values are
   fake. The rows still count in txn_count but contribute nothing to spend.
3. **Refund rows** → kept as behavioural features; points reversed in the
   adjusted column only.
4. **Orphan ids** → excluded: a churn score is only actionable for members we can
   profile and contact.
5. **Duplicate transactions** → deduped on transaction_id before any aggregation.

**Verification performed:** row count (49,996 = 50,010 deduped members minus 14
with zero transactions), member_id uniqueness asserted in code, the 150 ambiguous
members confirmed to have NULL tier, orphan ids confirmed absent. Median recency 86 days, mean 184 (a long tail of already-gone members); median
lifetime transactions 4; max lifetime spend $2,267 (before the range rule, the
fake batch made this read $100,293). The table also surfaced **21 members with negative adjusted
balances** — the 5 redeemed-more-than-earned anomalies from Part 1 plus members
whose refund reversals push them under: the feature table catching an audit issue.

**Features I'd be nervous shipping as-is:**

- `avg_days_between_txns` — guarded to ≥3 transactions but still noisy at exactly
  3 (one long gap dominates).
- `spend_trend_90d` — NULL for 70% of members *by design* (no prior-window spend:
  a new member is not "declining", they are unmeasurable). A model must treat that
  NULL as unmeasurable, not impute 1.0, or every new member looks stable.
- `refund_rate` — a valid signal today, but if the accrual bug is patched,
  historical and future refund behaviour become inconsistent: a silent
  train/serve skew. This connects to drift monitoring in Part 3.

---

## Part 3 — Pipeline Design

Context: daily runs, 20 brands, ~50x volume, feeding a feature store an ML model
reads every morning. Thesis: **every issue in the Part 1 audit entered through an
unguarded boundary, so the design enforces contracts at the boundary.**

```
 20 brand sources (daily)
        |
        v
+------------------+   schema fingerprint vs contract
|  INGESTION GATE  |   format-conformance %  |  enum domains
|  (Auto Loader)   |   range checks (catches the TO batch)
+---+----------+---+   PII: tokenize email / drop names / age-band DOB
    |          |
    | pass     | fail --> QUARANTINE + alert (load halts loudly)
    v
+------------------+
|  BRONZE (Delta)  |  raw, tokenized, MERGE on transaction_id
+------------------+  (replay-safe: reruns are no-ops)
    v
+------------------+
|  SILVER          |  dedup'd, conformed types, refunds flagged,
+------------------+  gross + adjusted points
    v
+------------------+   DLT expectations: row-count vs 30d baseline,
|  GOLD / FEATURES |   ref-integrity, freshness < 24h
+------------------+   fail --> serve YESTERDAY'S validated features
    v                          with a staleness flag
 Feature store --> churn model (06:00 SLA)
 Campaign list --> token vault --> CRM (only place PII reappears)

 Orchestration: Databricks Workflows, for-each over brand
```

### 1. Ingestion & idempotency

The 1,936 exact duplicate transactions in the raw extract are the signature of
non-idempotent ingestion — a replay or retry appended rows that already existed.
The design makes that failure class impossible rather than cleanable:

- **MERGE (upsert) on `transaction_id`** into Delta tables: insert if new, no-op
  if an identical row exists. Replays and retries become safe by construction.
- Same id, *different* content (the 59 conflict rows from Part 1) is not a replay
  — it routes to a quarantine table and alerts. Never silently overwrite.
- Secondary layer: daily partition overwrite (`replaceWhere`) for full-day
  reprocessing; ingestion watermarks tracked per source.

### 2. Schema drift detection

Evidence this pipeline needs it: the 3,000 MM/DD/YYYY dates (one source changed
format mid-stream — the column survived, the contract didn't) and the M9xxxxxx id
range (a second id-issuing source appeared). Three layers, checked on every load
**before** acceptance:

1. **Structural** — schema fingerprint (ordered column names + types) compared to
   the registered contract. Mismatch → halt, quarantine, alert with the diff.
   Never auto-map "probably the same column."
2. **Format** — per-column conformance rate vs expected pattern; a drop from 100%
   to 98.5% on `transaction_date` fires the day drift starts, not months later.
3. **Semantic** — enum domain checks on categoricals (a new tier value alerts),
   distribution checks on numerics (catches the next sentinel we haven't seen).

In Databricks terms: Auto Loader with `rescuedDataColumn` captures non-conforming
records instead of dropping them; DLT expectations enforce the value layer.

### 3. Data quality SLA & alerting

Promoted directly from the Part 1 prevention-control column. Data is "safe to
serve" only after passing, per load:

| Check | Threshold | On failure |
|---|---|---|
| PK uniqueness (transaction_id, member_id) | 100% | Fail load |
| Referential integrity (txn member_id in member dim) | ≥ 99.5% (guest ids routed to holding table) | Quarantine violations |
| Amount range (-1,000 to 10,000) + known sentinels | 0 violations | Quarantine + alert |
| Not-null rate per critical column | Within 2% of 30-day baseline | Alert |
| Row count per brand vs 30-day baseline | Within ±3σ | Alert (this is the check that catches Part 4's incident on day one) |
| Format conformance per column | ≥ 99.9% | Alert |
| Freshness (max txn_date lag) | < 24h | Block feature refresh, alert |

SLA to consumers: features refreshed by 06:00 daily; if quality gates fail, the
model serves **yesterday's validated features** with a staleness flag rather than
today's unvalidated ones. Alerting: hard failures page the on-call; soft
threshold breaches post to the team channel with the failing check and sample rows.

### 4. PII handling

The churn model uses **zero PII** — the 19-column feature table proves it
empirically. So the design is minimization, not protection:

| Field | Treatment | Where |
|---|---|---|
| email | **Tokenize**; token→email mapping in a restricted vault | At ingestion (bronze) |
| first_name, last_name | **Drop** from the warehouse entirely; vault only | At ingestion |
| birth_date | **Generalize** to age_band (25–34, …) as a feature; raw date to vault | At ingestion |
| member_id | Keep — already a surrogate | — |

Silver and gold layers never contain a raw identifier. The one legitimate PII
consumer — campaign activation (Part 7) — redeems tokens through the vault
service at send time under its own audited access. Analysts can build the
win-back list end to end without ever being *able* to see who is on it. Unity
Catalog fine-grained grants and column masks enforce this, but the architecture
(PII never travels) is the control; masking a column that shouldn't have arrived
is the weaker design. This is GDPR/DPDP-style minimization and purpose limitation.

### 5. Orchestration: Databricks Workflows

The workload is Spark transforms on Delta tables with DLT quality gates feeding a
Databricks feature store — all the work happens inside one platform, so the
orchestration question is whether to add a second platform just to press "go" on
the first. No:

- **Zero integration seam** — Airflow orchestrating Databricks means two systems,
  two auth setups, two failure domains, two places logs live. Workflows gives
  task-level retries, cluster reuse, and lineage in the same place as the data.
- **Our failure modes are native** — "if quality check fails, halt and alert" is
  a conditional task edge, not custom sensor code.
- **Scale story matches** — 20 brands is a for-each task over a brand parameter,
  not twenty copied DAGs.

**When I'd reverse this:** if the pipeline needed to orchestrate significant work
*outside* Databricks — many SaaS API pulls, non-Spark systems, teams on different
warehouses — Airflow's platform-neutral glue becomes the point. And dbt is not an
orchestrator; it's the transformation layer, and I'd happily run dbt inside a
Workflows task for SQL models. The three aren't really competitors here.

---

## Part 4 — Production Incident

`engagement_score` = 0 for ~40% of PulseEats members, same 6-day window, one
brand only. Stakeholder asks: real, or us?

**What the three clues already establish:**
- *One brand only* → brand-scoped cause: PulseEats' source feed or a
  brand-partitioned job. A shared-logic bug would hit all three brands.
- *~40%, not 100%* → not a dead feed (that's 100%), not noise. 40% is the
  signature of one **segment** — one channel, one region, one of several source
  files — going missing.
- *Fixed 6-day window, same boundaries for everyone* → real behaviour doesn't
  start and stop for thousands of people on the same two dates. Synchronized
  boundaries are machine behaviour: outage, deploy, credential expiry.

The core ambiguity: **absence of data and absence of activity look identical
after aggregation.** If engagement_score sums transactions, "no rows arrived" and
"no one shopped" both produce 0.

**Steps, in order:**

1. **Reply to the stakeholder first** (~30 seconds): "Checking whether this is a
   data issue before anyone escalates to the brand team; answer within the hour."
   Prevents a false alarm reaching a client.
2. **Raw-volume check — the fork.** I validate raw PulseEats transaction volume
   and completeness for those six days against adjacent periods:
   `SELECT DATE(txn_date), COUNT(*), SUM(amount) FROM transactions_raw WHERE
   brand='PulseEats' AND txn_date BETWEEN <window> GROUP BY 1` vs the same days
   ±2 weeks. **If raw activity is normal but the feature is zero, the problem is
   our pipeline; if raw activity itself collapsed, then investigate whether the
   business event was real.**
3. **Profile the affected 40%:** GROUP BY channel, country on affected vs
   unaffected members. 95% one channel or one region → segment identified, source
   found. Evenly spread → the failure is in the feature job, not a feed.
4. **Pipeline's own evidence:** Workflows run history for PulseEats tasks across
   the window; per-load row counts vs 30-day baseline; schema-drift and
   quarantine logs. (Under the Part 3 design this step is one dashboard.)
5. **Deploy/config change** dated to the window's start — job code, credentials,
   source API version.

**Hypotheses, ranked:**

1. **One PulseEats source feed silently failed for 6 days** (POS/app integration
   outage, expired credential, changed endpoint). Fits brand scoping, the ~40%
   segment, and machine-clean boundaries. Most likely by a distance.
2. **Feature-job bug scoped to PulseEats** — a filter or join change zeroing one
   brand; fits if step 3 shows no segment pattern and step 2 shows raw data
   present.
3. **Real engagement collapse** — possible (regional app outage, boycott), but
   real events don't produce identical zero-boundaries across thousands of
   members, and 40% with no segment logic is behaviorally implausible. Only
   survives if raw volume genuinely cratered *and* a segment/geography explains it.

**Resolution rule:** backfill the window idempotently (the MERGE design makes the
replay safe), then add the alert that would have caught it on day one: per-brand
row-count deviation vs trailing baseline. An incident that doesn't produce a new
check is a lesson wasted.

---

## Part 5 — Stakeholder Message (~150 words, forwardable)

> Quick update on the churn scores: **delivery will move from Wednesday to Friday
> this week.**
>
> During routine validation we found a small set of records in the latest source
> data that needed correction before scoring. Rather than release scores built on
> unverified inputs, we're correcting them and re-running the process.
> **Previously delivered scores are not affected.**
>
> We've also used this cycle to add automated quality checks to the pipeline, so
> incoming data is now validated before scoring runs — this delay is one-time,
> and future deliveries will be more reliable for it.
>
> No action needed on your side. I'll confirm as soon as Friday's scores are
> published.

Design choices: concrete dates in the first line (the only thing a skimming
reader retains); the past-scores reassurance stated before the client asks; the
delay reframed as a one-time cost of permanent quality gates; ends with what the
reader must do (nothing).

---

# SECTION 2 — Analytical Reasoning Track

## Part 6 — Which brand is actually the most generous?

**Marketing's claim is wrong — and inverted.** After cleaning, points earned per
dollar of real spend:

| Brand | Points per dollar (median; ratio-of-sums agrees) |
|---|---|
| PulseMart | **2.30** — most generous |
| PulseHome | 2.00 |
| PulseEats | **1.75** — least generous |

**Why marketing got it wrong:** on the raw file, ratio-of-sums
(`SUM(points)/SUM(amount)`) puts PulseMart at **0.906** — apparently the worst —
because the anomalous TO-batch rows (six of them at 999999.99, all assigned to
PulseMart) add ~$6M of fake spend to its denominator. A handful of rows out of
194,311 invert the entire ranking. Any
"points per dollar" figure computed on the raw extract is untrustworthy.

**What I cleaned/excluded to trust the answer:** deduplicated transaction_ids,
nulled the anomalous batch amounts via the range rule, restricted to positive
amounts (refunds excluded — a
"points per dollar of real spend" question is about purchases). Accrual is a
fixed per-brand constant (std of the ratio ≈ 0.01–0.02 within each brand).

**Confound check:** median points-per-dollar by brand × member country is flat
across every country — no currency effect; the rates are genuinely fixed
(PulseEats 1.75 / PulseHome 2.00 / PulseMart 2.30 everywhere).

**Caveat and confidence:** points-per-dollar only measures generosity if a point
is worth the same at every brand. No redemption-value data exists in these files
to verify that — if PulseEats points redeem at twice the value, the conclusion
could flip. Confidence: **high** on the accrual-rate ranking, **medium** on
"generosity" overall until redemption value is confirmed.

## Part 7 — Win-back campaign list (20 members)

**My definition of "worth winning back":** a member who used to spend a lot and
stopped recently but not long ago. A small offer to a past big spender pays for
itself when they return — and if the offer restarts the buying habit, they keep
purchasing with their own money afterward. Someone gone over ~9 months probably
won't come back for one offer; someone active in the last 3 months doesn't need
one.

**Criteria:** last purchase 90–270 days before 2026-06-30, lifetime spend in the
top quartile (≥ $351), at least 3 purchases → **4,157 candidates**. Ranked by
total spend (recoverable value), tiebreak by redemption rate — members who
redeemed before have shown the loyalty program itself matters to them, so a
points-based offer has a lever to pull.

**Excluded, and why:**
- `status = 'churned'` — the business already wrote them off; win-back targets
  the *slipping*, not the gone. (Used cautiously: Part 1 showed the status column
  carries conflicts.)
- Recency > 270 days — too cold; offer likely wasted.
- `refund_rate > 0.3` — buy-and-return behaviour: an offer feeds the pattern,
  and since refunds currently still earn points (Part 1), it is also an exploit
  invitation.
- M9xxxxxx guest ids — no contact details; nothing to send.

Selection code (runs on `feature_table.csv`):

```python
pool = f[(f.days_since_last_txn.between(90, 270))
         & (f.total_spend >= f.total_spend.quantile(.75))
         & (f.txn_count_lifetime >= 3)]
pool = pool[pool.refund_rate <= 0.3]
top20 = pool.sort_values(["total_spend", "redemption_rate"],
                         ascending=False).head(20)
```

Selected members (spend range $1,897–$2,267, recency 183–259 days):
M00219, M37512, M16790, M17011, M02751, M19556, M35047, M21752, M09531, M19853,
M08580, M05847, M15486, M08455, M05548, M14409, M03240, M33544, M25267, M04598.

Worth noting honestly: my first run of this selection returned six "members" with
$64K–$100K lifetime spend at the top of the list — all of them artifacts of the
anomalous TO batch (Part 1). Validating the campaign list against raw
transactions is what caught it; the list above is post-fix. Also notable: all 20
selected members have redemption_rate = 0 — the highest spenders in the lapse
window never engaged with points at all, which itself questions whether a
points-based offer is the right instrument for them versus a discount. I'd flag
that to the CRM team rather than hide it.

**Judgment call:** there is no single correct list. Mine optimizes for
*recoverable value with a loyalty lever*. An equally defensible list would target
members whose `spend_trend_90d` just collapsed while recency is still short —
catching them **earlier** in the slip. With more time I'd score both lists and
let campaign economics (offer cost vs expected recovered spend) pick.

## Part 8 — Outstanding points liability

**Headline figure: ≈ $229K** (22,944,750 net points × an assumed $0.01/point) —
with the assumptions and caveats below, which matter as much as the number.

**Computation** (after dedup and sentinel exclusion):
- Points earned: 25,323,669 (anomalous TO batch's 1,787 points excluded — an invalid load, not real accrual)
- Points redeemed: 2,378,919
- **Net outstanding: 22,944,750 points**

**The critical assumption:** no points-to-currency rate exists anywhere in the
data. I assume $0.01/point (a common loyalty benchmark) and show sensitivity:

| Rate | Liability |
|---|---|
| $0.005 / point | $114,724 |
| **$0.01 / point** | **$229,448** |
| $0.02 / point | $458,895 |

The true rate must come from finance/program terms before this touches a balance
sheet.

**What makes me distrust my own number (line-itemed, not hidden):**
1. **268,299 points sit on refunded purchases** — accrual that per policy should
   not exist (Part 1), but until clawed back these points are redeemable and
   arguably owed. Included, flagged as accrual-bug exposure.
2. **388,681 points belong to the M9xxxxxx guest/orphan ids** — the obligation
   exists independent of our ability to attribute it (a guest with a receipt is
   owed their points), but if investigation proves these are technically
   unredeemable, they become a breakage adjustment. Included, line-itemed as
   unattributed accrual. That is a finding to establish, not an assumption to
   start from.
3. **1,496 rows have null points_earned** — unknown, not zero; the true earned
   figure is slightly higher than computed.
4. **No expiry policy in the data.** Dormancy exposure is material and measured:
   **8,771 members dormant 12+ months hold 3,541,958 net points (~15% of the
   liability); 2,027 dormant 24+ months hold 688,154.**
   If points expire, that slice is where breakage lands. Standard
   accounting would also apply a breakage rate (expected never-redeemed share) —
   observed redemption here is only ~9.4% of points earned, which suggests
   breakage is large, but estimating it needs the program's expiry terms.

**Confidence:** high on the net points figure (it is arithmetic on cleaned data);
low-to-medium on the dollar figure until the redemption rate and expiry policy
are confirmed. A defensible figure with named caveats is the deliverable; a
confident figure without them would be wrong in at least two of the four ways
above.

---

# Decision Log

**How I worked:** I used an AI assistant (Claude) throughout, as the brief
permits. The pattern: I profiled the data with scripted checks, made the
judgment calls in discussion, and had code drafted to my decisions, which I then
ran and verified end to end on my machine (row counts, null rates, flag
behaviour, the final numbers). Key decisions and corrections:

- **Duplicate member_ids (Part 1):** My first instinct was "take the higher
  tier/active status." Stress-testing it surfaced the flaw: that rule assumes
  upgrades are more recent than downgrades — a directional bias with no evidence,
  and status may feed the churn label, so guessing wrong contaminates the target.
  Changed my call to flag-and-exclude (0.3% of members). I also questioned
  whether exclusion could affect the customer side (logins/points) and confirmed
  the distinction: analytical exclusion touches only the feature table, never
  operational systems.
- **Refund rows (Part 1/2):** My call: keep refunds as behavioural signal but
  reverse the points ("keep-as-is is worst — they'll keep ordering and returning
  and farming points"). Refined into gross + adjusted dual columns after
  realising churn modelling and liability accounting need opposite views of the
  same rows.
- **Orphan ids (Part 1/7/8):** My call to split treatment: drop from the churn
  model (can't contact them), include in liability ("the points should exist
  while the customer decides to come back tomorrow"). The guest-checkout
  hypothesis was mine; checking repeat rate per id (mostly one-offs) supported it.
- **Sentinel (Part 6):** AI-surfaced during profiling (repeated exact value =
  sentinel, the max of DECIMAL(8,2)); the decision to keep the rows as events
  while nulling amounts, and the three-layer prevention design, came out of our
  discussion.
- **Part 4:** My framing: check raw transaction volume for the window against
  adjacent periods first, and let that fork route the investigation (raw normal +
  feature zero → our pipeline; raw collapsed → possibly real).
- **Part 5:** My first draft avoided alarm language but lacked a concrete date
  and didn't pre-answer "are the scores you already sent me wrong?" — fixed both,
  and reframed the delay as a one-time cost of permanent quality gates.
- **The anomalous batch (found during validation, not the initial audit):** my
  first audit caught 6 rows at 999999.99 by exact-value check. Only when
  validating the *finished* feature table (inspecting members with implausible
  $100K lifetime spend) did the full 15-row TO-prefixed batch surface — 9 rows my
  sentinel list had missed, which had also contaminated the first draft of the
  Part 7 campaign list with six fake "whales". I replaced the exact-value rule
  with a range rule, regenerated every downstream number, and confirmed the Part
  6 ranking was unaffected. This is the strongest argument in this submission for
  layered checks: the range check catches the class; the sentinel list catches
  only the instances you've already seen.
- **Where AI's first output needed correction:** an early figure for points on
  refunded transactions (265,569) was computed before sentinel-row exclusion was
  applied consistently; re-running the audit script gave the correct 268,299,
  which is what this document uses. Verifying every number by re-running the
  scripts, rather than trusting conversation figures, caught it.
