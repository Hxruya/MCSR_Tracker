# query_benchmarks

The "Behind this page" panel. While a request is handled, every SQL statement and MongoDB command it runs is
recorded, and a button is added to the HTML page. The button opens a list of the queries in the order they ran,
with the engine, label, time, row count and full statement.

## How it hooks in

`app/db_sql.py` connects with `cursor_class(pymysql.cursors.DictCursor)`, which times each statement, and
`app/db_mongo.py` passes a `CommandListener` to `MongoClient`. The panel is added to the response after the
template has rendered, so no template changes are needed. Queries run outside a request (the import thread,
the setup scripts) aren't recorded.

To label a query, start the SQL with a comment (`/* Running score */ SELECT ...`) or pass MongoDB's `comment`
option. Unlabelled queries get a summary such as `SELECT from players, teams`.

Parameter values are never recorded: SQL shows its `%s` placeholders, MongoDB reads show their structure with
every value replaced by `?`, MongoDB writes show field names but not values, and the tables listed in
`QUERY_BENCHMARKS_HIDE` show no statement text at all.

## Settings

SiftStats sets these in `app/config.py`: the panel is on unless `.env` has `QUERY_PANEL=0`, SQL is labelled
MariaDB, and the `users` table is hidden.

| Key | Default | Meaning |
|---|---|---|
| `QUERY_BENCHMARKS_ENABLED` | `False` | Show the panel. |
| `QUERY_BENCHMARKS_HIDE` | `()` | Tables or collections whose statements are hidden; they are still counted and timed. |
| `QUERY_BENCHMARKS_SQL_LABEL` | `"SQL"` | Engine name shown for SQL statements, e.g. `"MariaDB"`. |
| `QUERY_BENCHMARKS_MAX_ENTRIES` | `200` | Entries kept per request; the totals still count every query. |

Adding `#show-queries` to a page's URL opens the panel on load, which is handy in the demo video.

The panel shows statement text to anyone who can see the page, so it's only meant for the local demo.
