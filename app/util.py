"""Small helpers shared by blueprints: paging, input parsing, LIKE escaping."""
import re
from datetime import date, datetime, timedelta, timezone

PER_PAGE = 20
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
NICKNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


class BadParam(ValueError):
    """A query parameter failed validation; the message is shown to the user."""


def parse_page(value):
    try:
        page = int(value or 1)
    except ValueError:
        raise BadParam("Page must be a whole number.")
    if page < 1 or page > 100000:
        raise BadParam("Page is out of range.")
    return page


def pages(total, page, per_page=PER_PAGE):
    last = max(1, -(-total // per_page))
    return {"page": page, "last": last, "total": total, "has_prev": page > 1, "has_next": page < last}


def normalise_uuid(value):
    """Return a 32-hex lowercase uuid (dashes allowed in input) or None."""
    v = (value or "").strip().lower().replace("-", "")
    return v if UUID_RE.match(v) else None


def like_escape(text):
    """Escape LIKE wildcards; use with the default '\\' escape character."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_date(value, name):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BadParam(f"{name} must be a date like 2026-01-31.")


def parse_date_range(args):
    """?from=&to= -> (start, end) dates (either may be None); raises BadParam if invalid or reversed."""
    start, end = parse_date(args.get("from"), "From"), parse_date(args.get("to"), "To")
    if start and end and start > end:
        raise BadParam("'From' must be on or before 'To'.")
    return start, end


def parse_season(value, valid_seasons):
    """'' / 'lifetime' -> None; otherwise an int that exists in valid_seasons."""
    if value in (None, "", "lifetime"):
        return None
    try:
        season = int(value)
    except ValueError:
        raise BadParam("Season must be a number or 'lifetime'.")
    if season not in valid_seasons:
        raise BadParam(f"Season {season} is not in the database.")
    return season


def day_bounds(start, end):
    """Inclusive date range -> (datetime start, datetime exclusive end)."""
    lo = datetime.combine(start, datetime.min.time()) if start else None
    hi = datetime.combine(end, datetime.min.time()) if end else None
    if hi is not None:
        hi = hi + timedelta(days=1)
    return lo, hi


def sql_filters(season=None, lo=None, hi=None, prefix=""):
    """Season and [lo, hi) date filters as (["col = %s", ...], [params]) for columns season_id / played_at,
    optionally table-qualified (prefix="ra." or "m.")."""
    where, params = [], []
    for clause, value in ((f"{prefix}season_id = %s", season), (f"{prefix}played_at >= %s", lo),
                          (f"{prefix}played_at < %s", hi)):
        if value is not None:
            where.append(clause)
            params.append(value)
    return where, params


def epoch_ms(dt):
    """Datetime (naive = UTC) -> epoch milliseconds, the x value of time charts."""
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)
