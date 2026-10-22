-- Materialised player statistics. Loaded after views.sql.
-- The views stay the single definition of the rules; these tables are stored copies of them, refreshed by
-- refresh_player_stats() when an import or restore finishes. Pages read here first and fall back to the
-- live view for a player who hasn't been refreshed yet.
-- Statements are separated by lines containing only $$.

CREATE TABLE IF NOT EXISTS mv_player_stats_lifetime (
    player_id INT UNSIGNED NOT NULL,
    games_played INT UNSIGNED NOT NULL,
    wins INT UNSIGNED NOT NULL,
    losses INT UNSIGNED NOT NULL,
    draws INT UNSIGNED NOT NULL,
    forfeits INT UNSIGNED NOT NULL,
    win_rate DECIMAL(6, 2) NULL,
    forfeit_rate DECIMAL(6, 2) NULL,
    total_elo_change INT NULL,
    refreshed_at DATETIME NOT NULL,
    PRIMARY KEY (player_id),
    CONSTRAINT fk_mvl_player FOREIGN KEY (player_id) REFERENCES players (player_id) ON DELETE CASCADE,
    CONSTRAINT ck_mvl_counts CHECK (wins + losses + draws = games_played AND forfeits <= losses)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
$$

CREATE TABLE IF NOT EXISTS mv_player_stats_season (
    player_id INT UNSIGNED NOT NULL,
    season_id SMALLINT UNSIGNED NOT NULL,
    games_played INT UNSIGNED NOT NULL,
    wins INT UNSIGNED NOT NULL,
    losses INT UNSIGNED NOT NULL,
    draws INT UNSIGNED NOT NULL,
    forfeits INT UNSIGNED NOT NULL,
    win_rate DECIMAL(6, 2) NULL,
    forfeit_rate DECIMAL(6, 2) NULL,
    total_elo_change INT NULL,
    refreshed_at DATETIME NOT NULL,
    PRIMARY KEY (player_id, season_id),
    CONSTRAINT fk_mvs_player FOREIGN KEY (player_id) REFERENCES players (player_id) ON DELETE CASCADE,
    CONSTRAINT fk_mvs_season FOREIGN KEY (season_id) REFERENCES seasons (season_id),
    CONSTRAINT ck_mvs_counts CHECK (wins + losses + draws = games_played AND forfeits <= losses)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
$$

-- Refresh one player (p_player_id) or everyone (NULL). Replaces that player's rows from the views.
CREATE OR REPLACE PROCEDURE refresh_player_stats(IN p_player_id INT UNSIGNED)
MODIFIES SQL DATA
BEGIN
    DELETE FROM mv_player_stats_lifetime WHERE p_player_id IS NULL OR player_id = p_player_id;
    DELETE FROM mv_player_stats_season WHERE p_player_id IS NULL OR player_id = p_player_id;
    INSERT INTO mv_player_stats_lifetime
        SELECT player_id, games_played, wins, losses, draws, forfeits, win_rate, forfeit_rate, total_elo_change,
               UTC_TIMESTAMP()
        FROM v_player_stats_lifetime WHERE p_player_id IS NULL OR player_id = p_player_id;
    INSERT INTO mv_player_stats_season
        SELECT player_id, season_id, games_played, wins, losses, draws, forfeits, win_rate, forfeit_rate,
               total_elo_change, UTC_TIMESTAMP()
        FROM v_player_stats_season WHERE p_player_id IS NULL OR player_id = p_player_id;
END
$$