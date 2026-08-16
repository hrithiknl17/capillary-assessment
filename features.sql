-- ============================================================
-- Part 2: Member-level feature table for 90-day churn model
-- Snapshot date: 2026-06-30 (max transaction date in the data)
--
-- Data quality handling carried in from Part 1:
--   1. Duplicate transaction_ids  -> dedup, keep one row per id
--   2. Anomalous batch (15 TO-prefixed rows, single timestamp, amounts
--      64,000-999,999.99) -> amounts nulled via RANGE rule (> $1,000);
--      rows kept as events. Exact-value sentinel check caught only 6/15;
--      the range check catches the whole class.
--   3. Refund rows (amount < 0)   -> kept as behavioural signal
--      (refund_count / refund_rate are features); points on refunds
--      kept in gross, reversed in adjusted balance
--   4. Orphan member_ids (M9xxxxxx, no member record) -> excluded:
--      churn scores are only actionable for members we can profile
--      and contact
--   5. Duplicate member_ids with conflicting tier/status -> member
--      kept, tier features nulled, is_tier_ambiguous flag set
--   6. Mixed date formats -> normalised upstream in Python before
--      load (SQLite date functions need ISO)
-- ============================================================

WITH params AS (
    SELECT DATE('2026-06-30') AS as_of
),

-- one row per transaction_id (Part 1 issue: 1,995 duplicate ids,
-- 1,936 fully identical). MIN(rowid) keeps the first occurrence.
txn_dedup AS (
    SELECT *
    FROM transactions_clean
    WHERE rowid IN (
        SELECT MIN(rowid)
        FROM transactions_clean
        GROUP BY transaction_id
    )
),

-- registered members only, sentinel amounts nulled
txn AS (
    SELECT
        t.member_id,
        t.txn_date,
        -- anomalous batch TO00000-TO00014 (one timestamp, absurd amounts):
        -- range rule, not exact-value list. Max legitimate amount is $600;
        -- anything above $1,000 is treated as unknown, row kept as an event.
        CASE WHEN t.amount > 1000 THEN NULL ELSE t.amount END AS amount,
        t.points_earned,
        t.points_redeemed
    FROM txn_dedup t
    WHERE t.member_id IN (SELECT member_id FROM members_dedup)
),

-- ---------- recency / frequency / monetary ----------
rfm AS (
    SELECT
        member_id,
        JULIANDAY((SELECT as_of FROM params)) - JULIANDAY(MAX(txn_date))
            AS days_since_last_txn,
        COUNT(*)                                        AS txn_count_lifetime,
        SUM(CASE WHEN txn_date >= DATE((SELECT as_of FROM params), '-90 days')
                 THEN 1 ELSE 0 END)                     AS txn_count_last_90d,
        SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS total_spend,
        AVG(CASE WHEN amount > 0 THEN amount END)        AS avg_order_value,
        SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END)      AS refund_count,
        CAST(SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS REAL)
            / COUNT(*)                                   AS refund_rate,
        -- avg gap between purchases: flagged "nervous to ship" below
        CASE WHEN COUNT(*) >= 3
             THEN (JULIANDAY(MAX(txn_date)) - JULIANDAY(MIN(txn_date)))
                  / (COUNT(*) - 1)
             ELSE NULL END                               AS avg_days_between_txns
    FROM txn
    GROUP BY member_id
),

-- ---------- engagement trend: last 90d vs the 90d before ----------
trend AS (
    SELECT
        member_id,
        SUM(CASE WHEN txn_date >= DATE((SELECT as_of FROM params), '-90 days')
                  AND amount > 0 THEN amount ELSE 0 END) AS spend_recent,
        SUM(CASE WHEN txn_date <  DATE((SELECT as_of FROM params), '-90 days')
                  AND txn_date >= DATE((SELECT as_of FROM params), '-180 days')
                  AND amount > 0 THEN amount ELSE 0 END) AS spend_prior
    FROM txn
    GROUP BY member_id
),

-- ---------- points behaviour (the redemption signal) ----------
points AS (
    SELECT
        member_id,
        SUM(COALESCE(points_earned, 0))                  AS points_earned_gross,
        -- adjusted: reverse accrual on refund rows (Part 1 decision:
        -- points granted on returns are a policy bug)
        SUM(CASE WHEN amount > 0 OR amount IS NULL
                 THEN COALESCE(points_earned, 0) ELSE 0 END)
                                                         AS points_earned_adj,
        SUM(points_redeemed)                             AS points_redeemed,
        MAX(CASE WHEN points_redeemed > 0 THEN txn_date END)
                                                         AS last_redemption_date
    FROM txn
    GROUP BY member_id
)

SELECT
    m.member_id,
    -- tier nulled for the 150 conflicting duplicates (Part 1 decision)
    CASE WHEN m.is_tier_ambiguous = 1 THEN NULL ELSE m.tier END AS tier,
    m.is_tier_ambiguous,
    m.join_date_missing,

    r.days_since_last_txn,
    r.txn_count_lifetime,
    r.txn_count_last_90d,
    r.avg_days_between_txns,          -- NULL for members with < 3 txns
    r.total_spend,
    r.avg_order_value,
    r.refund_count,
    ROUND(r.refund_rate, 4)                               AS refund_rate,

    -- trend NULL (not 0) when there is no prior-window spend:
    -- a new member is not "declining", they are unmeasurable
    CASE WHEN tr.spend_prior > 0
         THEN ROUND(tr.spend_recent / tr.spend_prior, 4)
         ELSE NULL END                                    AS spend_trend_90d,

    p.points_earned_gross,
    p.points_earned_adj,
    p.points_redeemed,
    p.points_earned_adj - p.points_redeemed               AS points_balance,
    CASE WHEN p.points_earned_adj > 0
         THEN ROUND(CAST(p.points_redeemed AS REAL) / p.points_earned_adj, 4)
         ELSE 0 END                                       AS redemption_rate,
    CASE WHEN p.last_redemption_date IS NOT NULL
         THEN JULIANDAY((SELECT as_of FROM params))
              - JULIANDAY(p.last_redemption_date)
         ELSE NULL END                                    AS days_since_last_redemption

FROM members_dedup m
JOIN rfm    r  ON r.member_id  = m.member_id
LEFT JOIN trend  tr ON tr.member_id = m.member_id
LEFT JOIN points p  ON p.member_id  = m.member_id;
