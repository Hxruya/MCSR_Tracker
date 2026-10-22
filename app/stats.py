"""Rate calculations, identical to setup/sql/routines.sql and setup/sql/views.sql.

win_rate = wins / (games_played - draws) * 100, forfeit_rate = forfeits / games_played * 100,
2 dp; None when the denominator is 0. Draws count as games played but not in the win-rate denominator.
"""
from decimal import ROUND_HALF_UP, Decimal

_4DP = Decimal("0.0001")
_2DP = Decimal("0.01")


def rate(part, games_played):
    """part / games_played * 100, rounded like MariaDB's ROUND(part * 100 / games, 2)."""
    if not games_played:
        return None
    # MariaDB evaluates the division at 4 extra decimal places (div_precision_increment), then ROUND(.., 2).
    value = (Decimal(part) * 100 / Decimal(games_played)).quantize(_4DP, rounding=ROUND_HALF_UP)
    return value.quantize(_2DP, rounding=ROUND_HALF_UP)


def win_rate(wins, games_played, draws):
    """wins / (games_played - draws) * 100, like the SQL function win_pct()."""
    return rate(wins, games_played - (draws or 0))


def format_rate(value):
    return "N/A" if value is None else f"{value:.2f}%"


def format_ms(ms):
    """Milliseconds -> mm:ss.mmm (minutes may exceed 59)."""
    if ms is None:
        return "N/A"
    ms = int(ms)
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{ms % 1000:03d}"


def format_gap(ms):
    """Signed time difference in seconds: -5749 -> '−5.7s' (ahead), 2594 -> '+2.6s' (behind)."""
    if ms is None:
        return "N/A"
    return f"{'+' if ms > 0 else '−' if ms < 0 else '±'}{abs(ms) / 1000:.1f}s"