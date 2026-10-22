-- Rate functions for the statistics views. Win rate leaves draws out of the denominator; both return NULL
-- when there is nothing to divide by. app/stats.py mirrors them and a test checks they agree.
-- Statements are separated by lines containing only $$.

CREATE OR REPLACE FUNCTION pct(part BIGINT, whole BIGINT)
RETURNS DECIMAL(6, 2)
DETERMINISTIC NO SQL
RETURN IF(whole IS NULL OR whole = 0, NULL, ROUND(COALESCE(part, 0) * 100 / whole, 2))
$$
CREATE OR REPLACE FUNCTION win_pct(wins BIGINT, games BIGINT, draws BIGINT)
RETURNS DECIMAL(6, 2)
DETERMINISTIC NO SQL
RETURN pct(wins, games - COALESCE(draws, 0))
$$