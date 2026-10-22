# MCSR Ranked Statistics Tracker (INF2003 Group 18)

A Flask web app that tracks [MCSR Ranked](https://mcsrranked.com) players. Match summaries are stored in MariaDB; seeds, videos, raw API data and timelines are stored in MongoDB.

You can search for players and load new ones on demand, then view their profile for lifetime and per-season stats, an Elo chart, End-game stats, best seed types, top rivals and split analysis. There is a filterable match history (each filter option shows its count), a match page that combines both databases, a head-to-head comparison with recent form, the official Elo leaderboard, a fastest-wins table, and a seed analysis that ranks every overworld and bastion combination. Logged-in users can keep bookmarks and private, searchable match notes. Every page has a "Behind this page" panel listing the MariaDB and MongoDB queries it ran and how long each took.

Contents:
- [1 Prerequisites](#1-prerequisites) 
- [2 Install the databases](#2-install-the-databases)
- [3 Create the database accounts](#3-create-the-database-accounts)
- [4 Set up the project](#4-set-up-the-project)
- [5 Create the schema](#5-create-the-schema)
- [6 Load the data](#6-load-the-data)
- [7 Run the app](#7-run-the-app)
- [8 Using the app](#8-using-the-app)
- [9 Tests](#9-tests)
- [10 Benchmarks](#10-benchmarks)
- [11 Troubleshooting](#11-troubleshooting)
- [12 Project layout](#12-project-layout)

 ---

## 1. Prerequisites

| Software | Version | Check with |
|---|---|---|
| Python | **3.14** (3.10+ should work) | `python --version` |
| MariaDB Server | **13.0.2** (10.6+ should work) | `mariadb --version` |
| MongoDB Community Server | **9.0.2** (7.0+ should work) | `mongod --version` |
| Internet Access | For importing data from `api.mcsrranked.com` | |

No API key is needed. The MCSR Ranked API allows 500 requests per 10 minutes per IP, and the app paces itself to at most 480 per 10 minutes (one call every 1.25 s), below that limit. It also follows the API's `RateLimit` and `Retry-After` headers if it gets close.

## 2. Install the databases

### Windows

1.  **MariaDB:** download the MSI from <https://mariadb.org/download/> and run it. Set a **root password** when asked, and leave "Install as service" ticked. The client is installed at `C:\Program Files\MariaDB <version>\bin\mariadb.exe`.

2.  **MongoDB:** download the MSI from <https://www.mongodb.com/try/download/community> and choose "Install MongoD as a Service". The default local setup has no login.  

### macOS (Homebrew)

```bash

brew  install  mariadb

brew  services  start  mariadb

brew  tap  mongodb/brew

brew  install  mongodb-community

brew  services  start  mongodb-community

```

On Homebrew, MariaDB's `root` user signs in with your macOS account: run `sudo mariadb` in step 3.

## 3. Create the database accounts

The app uses two MariaDB accounts, created by `setup/sql/accounts.sql`, and each can only touch the app database `mcsr` and the test database `mcsr_test`:

-  `mcsr_app` is what the website and importer run as. It can read and write rows, run the stored routines and make session-only temporary tables, but it can't create, change or drop tables, views or triggers.

-  `mcsr_admin` owns the schema: `init_db.py` (also run by `restore_data.py`) and the tests' setup use it to create the tables, views, routines and triggers.

Open a terminal in the project folder (the one containing this README), then open the MariaDB client as root:

- Windows (PowerShell): `& "C:\Program Files\MariaDB 13.0\bin\mariadb.exe" -u root -p` (adjust the version folder)

- macOS: `sudo mariadb`

At the `MariaDB [(none)]>` prompt, run the script, then give both accounts passwords of your own. Alternatively, you can include your password when running the script itself by editing the source file. By typing the password, we can ensure that no secrets are in any of the files committed to the repository:

```sql

SOURCE <path_to_folder>/mcsr_tracker/setup/sql/accounts.sql

ALTER  USER  'mcsr_app'@'localhost' IDENTIFIED BY  'your-app-password';

ALTER  USER  'mcsr_admin'@'localhost' IDENTIFIED BY  'your-admin-password';

exit

```

Check it works: `mariadb -u mcsr_app -p`, then type the app password. After step 5, `python setup/check_privileges.py` proves the restrictions: every MariaDB check should say OK.

MongoDB needs no setup: the app creates the `mcsr` and `mcsr_test` databases itself. To restrict MongoDB the same way (optional), `python setup/mongo_accounts.py` creates the matching roles and users; then set `MONGO_URI` and `MONGO_ADMIN_URI` in `.env`, enable `security.authorization` in `mongod.cfg` and restart MongoDB. The file's header has the details.

## 4. Set up the project

From the project folder (the one containing this README):

**Windows (PowerShell)**

```powershell

python -m venv .venv

.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt

Copy-Item .env.example .env

```

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or call `.\.venv\Scripts\python.exe` directly instead of `python`.

**macOS / Linux**

```bash

python3  -m  venv  .venv

source  .venv/bin/activate

python  -m  pip  install  -r  requirements.txt

cp  .env.example  .env

```

If there is a need to exit the virtual environment, enter the following:
```
deactivate
```

Now edit **`.env`** (it's git-ignored, so never commit it):

| Key | Set it to |
|---|---|
| `FLASK_SECRET_KEY` | Generate a long random string with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `MARIADB_PASSWORD` | Your `mcsr_app` password from Step 3 |
| `MARIADB_ADMIN_USER` / `MARIADB_ADMIN_PASSWORD` | Your `mcsr_admin` password from Step 3 |
| `MARIADB_HOST` / `MARIADB_PORT` / `MARIADB_USER` | `localhost` / `3306` / `mcsr_app` |
| `MARIADB_DATABASE` / `MARIADB_TEST_DATABASE` | `mcsr` / `mcsr_test` |
| `MONGO_URI` / `MONGO_DB` / `MONGO_TEST_DB` | `mongodb://localhost:27017` / `mcsr` / `mcsr_test` |
| `QUERY_PANEL` | Optional.  Using `0` hides the "Behind this page" panel |

The commands below assume the venv from Step 4 is active.

## 5. Create the schema

```bash

python  setup/init_db.py

```

This creates the MariaDB database, tables (`setup/sql/schema.sql`), stored functions, views, summary tables and triggers, plus the MongoDB collections with their validators and indexes (including the `elo_history` time-series collection). It's safe to run again at any time.

To **delete everything** and start over (it asks you to type `yes`):

```bash

python  setup/init_db.py  --reset

```

## 6. Load the data

### Restore the bundled data (under a minute)

The submission includes the exact dataset used for the report and demo in `data/dump/` (about 8 MB, compressed; exported 17 Oct 2026): the full match histories of 8 players (edcr, Infume, Feinberg, Pinne, Aquacorde, Hxruya, doogile, hackingnoises) with the latest 100 ranked timelines of each, plus the top 100 of the live leaderboard at import time as basic profiles. In total: 47,219 matches, 3,740 players and 780 match timelines. It works offline, with no API calls.

```bash

python  setup/restore_data.py

```

This creates the schema if needed and checks the counts against `data/dump/manifest.json` at the end. MariaDB is loaded in one transaction: if anything fails, nothing changes there. MongoDB is loaded after that, since it can't share a MariaDB transaction; if its part fails, run `--replace` again (it's safe to repeat). It refuses to run if the databases already hold match data; use `--replace` to overwrite (it asks you to type `yes`), which also replaces the old MariaDB rows inside that one transaction. Accounts, bookmarks and notes are never included in the dump.

To check that both databases hold the same matches (after a restore or an import): `python setup/check_consistency.py`. It only reads.

To export loaded matches in the database: `python setup/export_data.py`.

### Loading more players

Logged-in users can load any player from MCSR Ranked: search for them, then press **Load** (Section 8). The profile appears immediately and the full match history imports in the background. Imports are upserts, so re-running one never duplicates anything and repairs a match that was interrupted. A match that can't be stored is skipped and listed in that import's record (MongoDB `import_runs`).

## 7. Run the app

```bash

flask  --app  app  run

```

Open <http://localhost:5000>. Press Ctrl+C to stop. Add `--reload` while editing code. Don't use `--debug` outside development, because it shows tracebacks to the browser.

## 8. Using the app

| Page | What it does |
|---|---|
| Search (`/players`) | Search stored players by nickname (partial match) or UUID. Searching never calls the MCSR Ranked API. When logged in, a **Load "name" from MCSR Ranked** button fetches a player who isn't stored (or isn't fully loaded). The profile appears immediately, and the full match history imports in the background with a progress display. The leaderboard has the same Load button. |
| Profile (`/players/<uuid>`) | Ranked stats for lifetime or any season (dropdown). It shows games played, wins, losses, draws, forfeits, win rate, forfeit rate and Elo change. It also shows an Elo chart with a 20-match moving average (peak, biggest gain and loss, deepest slump), End-game stats, best overworld and bastion types, top rivals, per-season results, recent matches, fastest win, and the API's own totals for comparison. There is also a splits section which shows where the player has lost or made up the most time. **Refresh** (login required) fetches new matches that the player might have played. |
| Matches (`/matches`) | History filtered by player, season, type, result (win/loss/draw/forfeit/decay) and UTC date range. Each filter option shows how many matches it would return. |
| Match detail (`/matches/<id>`) | The MariaDB summary (players, Elo before and after, finish time) plus MongoDB seed details, Twitch VOD links and the split-time timeline. Opening the page never calls the MCSR Ranked API, each import loads the timelines of that player's latest 100 ranked matches. For any other match, a logged-in user can press **Load timeline** (fetched once, then cached for everyone). |
| Compare (`/compare`) | Two players side by side under the same season and date filters, and their head to head: score, streaks, Elo exchanged, running score, results by seed type and every meeting. **Recent Form** compares the latest 20 ranked matches with the previous 20 and needs at least 40. |
| Seeds (`/seeds`) | Ranks every overworld structure + bastion combination by average completed winning time, with median, fastest and number of wins, and a chart of seconds faster or slower than the overall average. Filter by structure, bastion, seed variation or match type; click a combination to list its matches. Also shows the End-game zero-cycle summary. |
| Leaderboard (`/leaderboard`, `/fastest`) | The official MCSR Ranked leaderboard (top 150, current or any season), fetched from the API and cached for 30 seconds (a finished season's for a day). It also contains a link to the player's profile. `/fastest` shows the players with the fastest completed ranked win (all seasons or per season). |
| Account Menu | **My Account** (`/account`): your note and bookmark counts, recent notes, tags and bookmarked players. You can also change your username or password, or delete your account (which also removes your bookmarks and notes). **My Notes** (`/notes`): private, tagged notes added from any match page; search them (full-text) or filter by tag, edit or delete them. Stored in MongoDB. **My Bookmarks** (`/bookmarks`): players bookmarked from their profile, each with an editable note (up to 255 characters). |

The Behind this page button in the bottom-right corner (for example "12 queries, 92 ms") opens a list of every database query the page ran: MariaDB or MongoDB, what it does, its time and rows, and the full statement. Add `#show-queries` to a page's URL to open it straight away. Account lookups are hidden, and parameter values are never shown. Set `QUERY_PANEL=0` in `.env` to turn it off.

How stats are counted:

- Only ranked matches count, and "decay" entries are excluded. A decay is an inactivity Elo penalty that MCSR Ranked lists as a match with one player and no result. It is stored as its own result `decay`, never as a draw, so a Draw always means a real draw.

- Win rate is wins / (games − draws) × 100, so draws are left out of it. Forfeit rate is forfeits / games × 100. Both are to 2 decimal places; with nothing to divide by, they show N/A.

- A forfeit belongs to the player who gave up.

- Opponents who haven't been searched have basic profiles, so their stats only include matches against searched players.

## 9. Tests

```bash

python  -m  pytest  -q

```

Run it from the project folder: `pytest.ini` points pytest at `tests/`. The tests create and drop separate databases (`MARIADB_TEST_DATABASE` / `MONGO_TEST_DB`) and never touch your real data. They make no network calls. All 93 key tests should pass. pytest itself reports 116 because a few tests run once per input (for example, each invalid filter URL). They are grouped by what they demonstrate:

| File | Tests | What it checks |
|---|---|---|
| `test_crud.py` | 7 | Create, read, update, delete and search: bookmarks (MariaDB) and notes (MongoDB); changing username and password; deleting an account with its bookmarks and notes |
| `test_db_security.py` | 8 | Least-privilege accounts, ownership inside every query, account settings needing login, CSRF and the user's own password, login lockout after 5 failed attempts, SQL-injection attempts, hashed passwords, data minimisation |
| `test_integrity.py` | 7 | Triggers, CHECK / foreign-key / unique constraints, MongoDB validators (notes, and seed types on matches), all-or-nothing restore and export |
| `test_queries.py` | 14 | The statistics views and rate rules, window functions, aggregates, the seed ranking, Elo, End-game and split-analysis queries |
| `test_sql_vs_nosql.py` | 5 | MariaDB and MongoDB give the same answers; the two stores stay consistent, including notes whose user or match is gone |
| `test_optimisation.py` | 9 | Every optimisation (summary tables, deferred joins, index walk, merged views, caching) returns identical results; the profile's MongoDB pipelines use their indexes |
| `test_import.py` | 17 | API data to rows and documents, idempotent imports (re-imports keep `first_imported_at`), Refresh (including retrying a partial import), timelines, failures, the API client's pacing |
| `test_web_app.py` | 21 | Login and sessions, pages (including the profile's Splits section), the Load / Refresh / Load timeline buttons, the leaderboard, outages |
| `test_query_benchmarks.py` | 5 | The "Behind this page" query panel |

## 10. Benchmarks

```bash

python  benchmarks/benchmark.py  --player  edcr  --rival  Infume

```

This needs imported data. It runs each MariaDB-vs-MongoDB comparison 30 times after 3 warm-up runs and rewrites the results section of `benchmarks/benchmarks.md` (median, p95 and EXPLAIN output). That includes section 7 of `benchmarks.md`, where four questions are answered in both databases and checked for identical results. It never changes your actual data, the write test uses a temporary table and the test database.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `Can't connect to MySQL server on 'localhost'` / `connection refused` | MariaDB isn't running. Windows: `Start-Service MariaDB`; macOS: `brew services start mariadb`; Linux: `sudo systemctl start mariadb`. Check `MARIADB_PORT`. |
| `Access denied for user 'mcsr_app'@'localhost'` (or `'mcsr_admin'`) | That account's password in `.env` (`MARIADB_PASSWORD` or `MARIADB_ADMIN_PASSWORD`) doesn't match. As root: `ALTER USER 'mcsr_app'@'localhost' IDENTIFIED BY '<the .env password>';`. Make sure the value in `.env` has no quotes or trailing spaces. |
| `Access denied` or `command denied` while running `init_db.py` or the tests | Schema work needs `mcsr_admin`: check `MARIADB_ADMIN_USER` and `MARIADB_ADMIN_PASSWORD` in `.env` (step 4), and that `accounts.sql` ran (step 3). |
| `Access denied ... to database 'mcsr'` | The database names in `.env` must match the `GRANT`s in step 3. |
| `ServerSelectionTimeoutError` / `localhost:27017: connection refused` | MongoDB isn't running. Windows: `Start-Service MongoDB`; macOS: `brew services start mongodb-community`; Linux: `sudo systemctl start mongod`. |
| `RuntimeError: FLASK_SECRET_KEY is not set` | `.env` is missing or the key is empty (section 4). |
| Importer logs `HTTP 429` | The API rate limit (500 per 10 min) was hit, for example by several imports at once. The client waits (`Retry-After` / `RateLimit` header) and retries up to 3 times. Wait a few minutes and re-run: imports are safe to repeat. |
| Importer logs `network error ... getaddrinfo failed` | DNS or internet problem. It retries automatically; if it persists, check your connection and `MCSR_API_BASE`. |
| Profile says "The last import was interrupted" | The server stopped mid-import. Log in and press **Refresh** to resume. |
| `Document failed validation` for a match | The API sent a shape the validator doesn't expect. The match is skipped and listed in that import's record (MongoDB `import_runs`, `errors`). Report it with the match id. |
| Leaderboard says "can't be reached right now" | It is fetched live from `api.mcsrranked.com`, so the server needs internet access. The other pages keep working from stored data. |
| Charts don't appear (tables still work) | Charts load Chart.js from `cdnjs.cloudflare.com`, so the browser needs internet access. Every chart has a "Show data table" fallback. |

## 12. Project layout
  
```
mcsr_tracker/
├── app/                      the Flask web app
│   ├── __init__.py           creates the app, registers the pages
│   ├── players.py, matches.py, compare.py, seeds.py, leaderboard.py
│   │                         one file per page (routes + their queries)
│   ├── auth.py, account.py, bookmarks.py, notes.py
│   │                         login, the account page, bookmarks (MariaDB), notes (MongoDB)
│   ├── stats.py, summary.py, elo.py, endgame.py, splits.py, rivalry.py, facets.py
│   │                         shared statistics and queries used by several pages
│   ├── db_sql.py, db_mongo.py, cache.py, config.py, util.py
│   │                         database connections, caching, settings, small helpers
│   ├── importer/             loads data from the MCSR Ranked API (Load and Refresh)
│   ├── query_benchmarks/     the "Behind this page" query panel
│   ├── templates/            HTML pages
│   └── static/               CSS and JavaScript
├── setup/                    scripts you run once or occasionally
│   ├── init_db.py            create the tables, views, triggers, MongoDB collections (step 5)
│   ├── restore_data.py       load the bundled dataset (step 6)
│   ├── export_data.py        save your databases as a new dataset
│   ├── check_consistency.py  check both databases hold the same matches
│   ├── check_privileges.py   check what the app account may and may not do
│   ├── mongo_accounts.py     optional: least-privilege MongoDB accounts
│   └── sql/                  the SQL that init_db.py runs, plus accounts.sql (step 3)
├── data/dump/                the bundled dataset (section 6)
├── benchmarks/               MariaDB vs MongoDB benchmarks and their results (section 10)
├── tests/                    the test suite, one file per area (section 9)
└── extras/                   ER diagram and design notes
```

Data is © MCSR Ranked and its players. The Web Application's theme is inspired by Minecraft's Sift dimension.