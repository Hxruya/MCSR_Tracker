-- MCSR Ranked Statistics Tracker: MariaDB schema (InnoDB, utf8mb4).
-- Idempotent: safe to run repeatedly. Statements are separated by ';' at end of line.

CREATE TABLE IF NOT EXISTS users (
    user_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    username VARCHAR(30) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT uq_users_username UNIQUE (username),
    CONSTRAINT ck_users_username CHECK (username REGEXP '^[A-Za-z0-9_]{3,30}$')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS seasons (
    season_id SMALLINT UNSIGNED NOT NULL,
    label VARCHAR(20) NOT NULL,
    start_at DATETIME NULL,
    end_at DATETIME NULL,
    PRIMARY KEY (season_id),
    CONSTRAINT ck_seasons_dates CHECK (start_at IS NULL OR end_at IS NULL OR end_at > start_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Lookup for matches.match_type based on the API's numeric codes, named in the database and app.
CREATE TABLE IF NOT EXISTS match_types (
    match_type TINYINT UNSIGNED NOT NULL,
    label VARCHAR(10) NOT NULL,
    PRIMARY KEY (match_type),
    CONSTRAINT uq_match_types_label UNIQUE (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO match_types (match_type, label) VALUES (1, 'Casual'), (2, 'Ranked'), (3, 'Private'), (4, 'Event');

CREATE TABLE IF NOT EXISTS players (
    player_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    uuid CHAR(32) NOT NULL,
    nickname VARCHAR(32) NOT NULL,
    current_elo INT NULL,
    elo_rank INT UNSIGNED NULL,
    country CHAR(2) NULL,
    profile_status VARCHAR(10) NOT NULL DEFAULT 'minimal',
    profile_fetched_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (player_id),
    CONSTRAINT uq_players_uuid UNIQUE (uuid),
    CONSTRAINT ck_players_uuid CHECK (uuid REGEXP '^[0-9a-f]{32}$'),
    CONSTRAINT ck_players_status CHECK (profile_status IN ('minimal', 'full')),
    KEY ix_players_nickname (nickname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS player_season_results (
    player_id INT UNSIGNED NOT NULL,
    season_id SMALLINT UNSIGNED NOT NULL,
    final_elo INT NULL,
    final_rank INT UNSIGNED NULL,
    highest_elo INT NULL,
    lowest_elo INT NULL,
    phase_points INT NULL,
    PRIMARY KEY (player_id, season_id),
    CONSTRAINT fk_psr_player FOREIGN KEY (player_id) REFERENCES players (player_id) ON DELETE CASCADE,
    CONSTRAINT fk_psr_season FOREIGN KEY (season_id) REFERENCES seasons (season_id),
    CONSTRAINT ck_psr_elo CHECK (highest_elo IS NULL OR lowest_elo IS NULL OR highest_elo >= lowest_elo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS matches (
    match_id BIGINT UNSIGNED NOT NULL,
    season_id SMALLINT UNSIGNED NOT NULL,
    match_type TINYINT UNSIGNED NOT NULL,
    played_at DATETIME NOT NULL,
    duration_ms INT UNSIGNED NULL,
    -- 'decay' is an inactivity Elo penalty the API lists as a match (one participant, no result).
    result_type VARCHAR(10) NOT NULL,
    is_draw TINYINT(1) AS (result_type = 'draw') VIRTUAL,
    is_forfeit TINYINT(1) AS (result_type = 'forfeit') VIRTUAL,
    is_decayed TINYINT(1) AS (result_type = 'decay') VIRTUAL,
    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (match_id),
    CONSTRAINT fk_matches_season FOREIGN KEY (season_id) REFERENCES seasons (season_id),
    CONSTRAINT fk_matches_type FOREIGN KEY (match_type) REFERENCES match_types (match_type),
    CONSTRAINT ck_matches_result CHECK (result_type IN ('completed', 'forfeit', 'draw', 'decay')),
    KEY ix_matches_season_played (season_id, played_at),
    KEY ix_matches_played (played_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS match_players (
    match_id BIGINT UNSIGNED NOT NULL,
    player_id INT UNSIGNED NOT NULL,
    elo_before INT NULL,
    elo_change INT NULL,
    elo_after INT AS (elo_before + elo_change) VIRTUAL,
    outcome VARCHAR(5) NOT NULL,
    forfeited TINYINT(1) NULL,
    finish_time_ms INT UNSIGNED NULL,
    -- The match id on the winner's row, NULL on every other row: the UNIQUE key below allows any number of NULLs
    -- but one value per match, so a match has at most one winner, and the winner is by definition a participant.
    winner_match_id BIGINT UNSIGNED AS (IF(outcome = 'win', match_id, NULL)) VIRTUAL,
    PRIMARY KEY (match_id, player_id),
    CONSTRAINT fk_mp_match FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE,
    CONSTRAINT fk_mp_player FOREIGN KEY (player_id) REFERENCES players (player_id),
    CONSTRAINT ck_mp_outcome CHECK (outcome IN ('win', 'loss', 'draw', 'decay')),
    CONSTRAINT ck_mp_finish CHECK (finish_time_ms IS NULL OR outcome = 'win'),
    CONSTRAINT ck_mp_forfeit CHECK (forfeited IS NULL OR forfeited = 0 OR outcome = 'loss'),
    KEY ix_mp_player_match (player_id, match_id),
    KEY ix_mp_finish (finish_time_ms),
    CONSTRAINT uq_mp_one_winner UNIQUE (winner_match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS bookmarks (
    bookmark_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id INT UNSIGNED NOT NULL,
    player_id INT UNSIGNED NOT NULL,
    note VARCHAR(255) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bookmark_id),
    CONSTRAINT uq_bookmarks_user_player UNIQUE (user_id, player_id),
    CONSTRAINT fk_bookmarks_user FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    CONSTRAINT fk_bookmarks_player FOREIGN KEY (player_id) REFERENCES players (player_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;