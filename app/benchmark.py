"""Elo Tier Peer Benchmark.

Compares a player's median winning pace against all other players in the same Elo tier
(Netherite, Diamond, Emerald, Gold, Iron/Coal). Demonstrates advanced MariaDB SQL:
- Common Table Expressions (CTEs: WITH ... AS (...))
- Analytic window functions: PERCENTILE_CONT(0.50), PERCENTILE_CONT(0.25), PERCENTILE_CONT(0.75)
- Categorical bucketing via CASE expressions
- Dynamic percentile ranking within the tier
"""
from app.db_sql import query_one


def peer_benchmark(player_id, season=None):
    """Benchmark a player's median winning time against their Elo tier peer group.

    Returns None if the player has no current Elo, no completed wins, or the tier
    has insufficient data.
    """
    if season is None:
        season_join = ""
        season_cond = ""
        params = (player_id, player_id)
        elo_expr = "p.current_elo"
    else:
        season_join = "LEFT JOIN player_season_results psr ON psr.player_id = p.player_id AND psr.season_id = %s"
        season_cond = "AND w.season_id = %s"
        params = (season, player_id, season, player_id, season)
        elo_expr = "COALESCE(psr.final_elo, psr.highest_elo, p.current_elo)"

    sql = f"""
    /* Elo Tier Benchmark: peer pace comparison using window functions and CTE */
    WITH tier_assignment AS (
        SELECT 
            p.player_id,
            {elo_expr} AS effective_elo,
            CASE 
                WHEN {elo_expr} >= 1800 THEN 'Netherite (1800+)'
                WHEN {elo_expr} >= 1500 THEN 'Diamond (1500–1799)'
                WHEN {elo_expr} >= 1200 THEN 'Emerald (1200–1499)'
                WHEN {elo_expr} >= 900 THEN 'Gold (900–1199)'
                ELSE 'Iron / Coal (<900)'
            END AS tier_name,
            CASE 
                WHEN {elo_expr} >= 1800 THEN 'netherite'
                WHEN {elo_expr} >= 1500 THEN 'diamond'
                WHEN {elo_expr} >= 1200 THEN 'emerald'
                WHEN {elo_expr} >= 900 THEN 'gold'
                ELSE 'iron'
            END AS tier_badge
        FROM players p
        {season_join}
        WHERE {elo_expr} IS NOT NULL
    ),
    target_player AS (
        SELECT player_id, effective_elo, tier_name, tier_badge
        FROM tier_assignment
        WHERE player_id = %s
    ),
    tier_runs AS (
        SELECT ta.player_id, ta.tier_name, w.finish_time_ms
        FROM v_completed_wins w
        JOIN tier_assignment ta ON ta.player_id = w.player_id
        JOIN target_player tp ON tp.tier_name = ta.tier_name
        WHERE w.match_type = 2 {season_cond}
    ),
    tier_stats AS (
        SELECT 
            COUNT(*) AS tier_runs,
            COUNT(DISTINCT player_id) AS tier_players,
            MIN(finish_time_ms) AS tier_best_ms,
            MAX(med) AS tier_median_ms,
            MAX(q1) AS tier_q1_ms,
            MAX(q3) AS tier_q3_ms
        FROM (
            SELECT player_id, finish_time_ms,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY finish_time_ms) OVER () AS med,
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY finish_time_ms) OVER () AS q1,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY finish_time_ms) OVER () AS q3
            FROM tier_runs
        ) t
    ),
    player_runs AS (
        SELECT w.finish_time_ms
        FROM v_completed_wins w
        WHERE w.player_id = %s AND w.match_type = 2 {season_cond}
    ),
    player_stats AS (
        SELECT 
            COUNT(*) AS player_runs,
            MIN(finish_time_ms) AS player_best_ms,
            MAX(med) AS player_median_ms
        FROM (
            SELECT finish_time_ms,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY finish_time_ms) OVER () AS med
            FROM player_runs
        ) p
    )
    SELECT 
        tp.player_id,
        tp.effective_elo AS current_elo,
        tp.tier_name,
        tp.tier_badge,
        ps.player_runs,
        ps.player_best_ms,
        ps.player_median_ms,
        ts.tier_runs,
        ts.tier_players,
        ts.tier_best_ms,
        ts.tier_median_ms,
        ts.tier_q1_ms,
        ts.tier_q3_ms,
        (ps.player_median_ms - ts.tier_median_ms) AS delta_ms,
        CASE 
            WHEN ps.player_median_ms < ts.tier_median_ms THEN 'Overperforming'
            WHEN ps.player_median_ms > ts.tier_median_ms THEN 'Underperforming'
            ELSE 'Average'
        END AS pace_status,
        (
            SELECT ROUND(COUNT(*) * 100.0 / NULLIF(ts.tier_runs, 0), 1)
            FROM tier_runs tr
            WHERE tr.finish_time_ms >= ps.player_median_ms
        ) AS percentile_rank
    FROM target_player tp
    CROSS JOIN tier_stats ts
    CROSS JOIN player_stats ps;
    """

    row = query_one(sql, params)
    if (
        not row
        or not row.get("player_runs")
        or not row.get("tier_runs")
        or row.get("player_median_ms") is None
        or row.get("tier_median_ms") is None
    ):
        return None

    return {
        "tier_name": row["tier_name"],
        "tier_badge": row["tier_badge"],
        "current_elo": row["current_elo"],
        "player_runs": int(row["player_runs"]),
        "player_best_ms": int(row["player_best_ms"]) if row["player_best_ms"] is not None else None,
        "player_median_ms": float(row["player_median_ms"]),
        "tier_runs": int(row["tier_runs"]),
        "tier_players": int(row["tier_players"]),
        "tier_best_ms": int(row["tier_best_ms"]) if row["tier_best_ms"] is not None else None,
        "tier_median_ms": float(row["tier_median_ms"]),
        "tier_q1_ms": float(row["tier_q1_ms"]) if row["tier_q1_ms"] is not None else None,
        "tier_q3_ms": float(row["tier_q3_ms"]) if row["tier_q3_ms"] is not None else None,
        "delta_ms": float(row["delta_ms"]) if row["delta_ms"] is not None else None,
        "pace_status": row["pace_status"],
        "percentile_rank": float(row["percentile_rank"]) if row["percentile_rank"] is not None else None,
    }
