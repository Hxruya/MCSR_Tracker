-- Statistics views. Only ranked (match_type = 2), non-decay matches count (decays have result_type 'decay').
-- Statements are separated by ';' at the end of a line.
-- Rates: wins / (games_played - draws) * 100 via win_pct(), and forfeits / games_played * 100 via pct(),
-- rounded to 2 dp; NULL when the denominator is 0 (setup/sql/routines.sql). app/stats.py mirrors them.

CREATE OR REPLACE VIEW v_ranked_match_players AS
SELECT mp.match_id, mp.player_id, mp.elo_before, mp.elo_change, mp.elo_after,
       mp.outcome, mp.forfeited, mp.finish_time_ms,
       m.season_id, m.played_at, m.result_type
FROM match_players mp
JOIN matches m ON m.match_id = mp.match_id
WHERE m.match_type = 2 AND m.is_decayed = 0;

CREATE OR REPLACE VIEW v_player_stats_lifetime AS
SELECT p.player_id, p.uuid, p.nickname,
       COUNT(r.match_id) AS games_played,
       COALESCE(SUM(r.outcome = 'win'), 0) AS wins,
       COALESCE(SUM(r.outcome = 'loss'), 0) AS losses,
       COALESCE(SUM(r.outcome = 'draw'), 0) AS draws,
       COALESCE(SUM(r.forfeited = 1), 0) AS forfeits,
       win_pct(SUM(r.outcome = 'win'), COUNT(r.match_id), SUM(r.outcome = 'draw')) AS win_rate,
       pct(SUM(r.forfeited = 1), COUNT(r.match_id)) AS forfeit_rate,
       SUM(r.elo_change) AS total_elo_change
FROM players p
LEFT JOIN v_ranked_match_players r ON r.player_id = p.player_id
GROUP BY p.player_id, p.uuid, p.nickname;

CREATE OR REPLACE VIEW v_player_stats_season AS
SELECT p.player_id, p.uuid, p.nickname, r.season_id,
       COUNT(r.match_id) AS games_played,
       SUM(r.outcome = 'win') AS wins,
       SUM(r.outcome = 'loss') AS losses,
       SUM(r.outcome = 'draw') AS draws,
       COALESCE(SUM(r.forfeited = 1), 0) AS forfeits,
       win_pct(SUM(r.outcome = 'win'), COUNT(r.match_id), SUM(r.outcome = 'draw')) AS win_rate,
       pct(SUM(r.forfeited = 1), COUNT(r.match_id)) AS forfeit_rate,
       SUM(r.elo_change) AS total_elo_change
FROM v_ranked_match_players r
JOIN players p ON p.player_id = r.player_id
GROUP BY p.player_id, p.uuid, p.nickname, r.season_id;

-- Every completed (non-forfeit, non-decay) win with its finish time, any match type: the single definition of
-- "a timed win". Ranked-only callers add match_type = 2.
-- A plain filtered join, so MariaDB merges it into the calling query (ALGORITHM=MERGE, no temp table).
CREATE OR REPLACE ALGORITHM = MERGE VIEW v_completed_wins AS
SELECT mp.match_id, mp.player_id, mp.finish_time_ms, m.season_id, m.played_at, m.match_type
FROM match_players mp
JOIN matches m ON m.match_id = mp.match_id
WHERE m.is_decayed = 0 AND m.result_type = 'completed'
  AND mp.outcome = 'win' AND mp.finish_time_ms IS NOT NULL;

-- Best completed (non-forfeit) winning time per player per season.
CREATE OR REPLACE VIEW v_fastest_wins AS
SELECT player_id, season_id, match_id, finish_time_ms, played_at
FROM (
    SELECT w.player_id, w.season_id, w.match_id, w.finish_time_ms, w.played_at,
           ROW_NUMBER() OVER (PARTITION BY w.player_id, w.season_id
                              ORDER BY w.finish_time_ms, w.match_id) AS rn
    FROM v_completed_wins w
    WHERE w.match_type = 2
) ranked_wins
WHERE rn = 1;

-- Best completed winning time per player across all seasons.
CREATE OR REPLACE VIEW v_fastest_wins_lifetime AS
SELECT player_id, season_id, match_id, finish_time_ms, played_at
FROM (
    SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.player_id ORDER BY f.finish_time_ms, f.match_id) AS rn
    FROM v_fastest_wins f
) best
WHERE rn = 1;

-- One row per match for lists: result, winner and the participants' nicknames, winner first.
CREATE OR REPLACE ALGORITHM = MERGE VIEW v_match_summary AS
SELECT m.match_id, m.played_at, m.season_id, m.match_type, m.result_type, m.duration_ms, m.is_decayed,
       w.nickname AS winner, w.uuid AS winner_uuid,
       (SELECT COUNT(*) FROM match_players x WHERE x.match_id = m.match_id) AS n_players,
       (SELECT GROUP_CONCAT(p.nickname ORDER BY x.outcome = 'win' DESC, p.nickname SEPARATOR '\t')
        FROM match_players x JOIN players p ON p.player_id = x.player_id WHERE x.match_id = m.match_id) AS names
FROM matches m
-- the winner's row, via uq_mp_one_winner
LEFT JOIN match_players wx ON wx.winner_match_id = m.match_id
LEFT JOIN players w ON w.player_id = wx.player_id;