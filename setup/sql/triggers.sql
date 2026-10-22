-- Cross-table integrity that CHECK constraints cannot express.
-- A participant row must agree with its match: outcome 'draw' <=> matches.result_type = 'draw', and
-- outcome 'decay' <=> result_type 'decay'.
-- (The winner needs no trigger: it is the participant row with outcome 'win', and the UNIQUE key on
-- match_players.winner_match_id allows at most one per match.)
-- Statements are separated by lines containing only $$ (bodies contain ';').

CREATE OR REPLACE TRIGGER trg_match_players_bi
BEFORE INSERT ON match_players
FOR EACH ROW
BEGIN
    IF EXISTS (SELECT 1 FROM matches m WHERE m.match_id = NEW.match_id
               AND ((NEW.outcome = 'draw') <> (m.result_type = 'draw')
                    OR (NEW.outcome = 'decay') <> (m.result_type = 'decay'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'match_players: outcome draw/decay must match a draw/decay result';
    END IF;
END
$$

CREATE OR REPLACE TRIGGER trg_match_players_bu
BEFORE UPDATE ON match_players
FOR EACH ROW
BEGIN
    IF EXISTS (SELECT 1 FROM matches m WHERE m.match_id = NEW.match_id
               AND ((NEW.outcome = 'draw') <> (m.result_type = 'draw')
                    OR (NEW.outcome = 'decay') <> (m.result_type = 'decay'))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'match_players: outcome draw/decay must match a draw/decay result';
    END IF;
END
$$

-- A match cannot become a draw or decay (or stop being one) while its stored participant rows say otherwise.
CREATE OR REPLACE TRIGGER trg_matches_bu
BEFORE UPDATE ON matches
FOR EACH ROW
BEGIN
    IF OLD.result_type <> NEW.result_type THEN
        IF EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id = NEW.match_id
                   AND ((mp.outcome = 'draw') <> (NEW.result_type = 'draw')
                        OR (mp.outcome = 'decay') <> (NEW.result_type = 'decay'))) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'matches: new result contradicts stored match_players rows';
        END IF;
    END IF;
END
$$