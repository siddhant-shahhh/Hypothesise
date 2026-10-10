#!/usr/bin/env bash
# Build the test target for a real Copilot run.
#
# The fake-secret FILES live in the repo at testbed/. The SQLite database does
# not: SQLite fails with "disk I/O error" on this external drive, the same
# constraint that put the main database in ~/.hypothesise (ADR-013). So the database
# goes in your home directory and the MCP server is pointed at it there.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(pwd)"
DB_DIR="$HOME/hypothesise-testbed"
DB="$DB_DIR/devdata.db"

mkdir -p "$DB_DIR"
rm -f "$DB"

python3 - "$DB" <<'PY'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT, created TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL, status TEXT);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor TEXT, action TEXT, ts TEXT);
""")
c.executemany("INSERT INTO users VALUES (?,?,?,?)", [
    (1, "ada@example.test", "Ada Lovelace", "2026-01-04"),
    (2, "alan@example.test", "Alan Turing", "2026-02-11"),
    (3, "grace@example.test", "Grace Hopper", "2026-03-22"),
    (4, "katherine@example.test", "Katherine Johnson", "2026-04-08"),
])
c.executemany("INSERT INTO orders VALUES (?,?,?,?)", [
    (1, 1, 129.99, "shipped"), (2, 1, 49.50, "pending"),
    (3, 2, 899.00, "shipped"), (4, 3, 15.25, "cancelled"),
    (5, 4, 240.00, "shipped"),
])
c.executemany("INSERT INTO audit_log VALUES (?,?,?,?)", [
    (1, "ada@example.test", "login", "2026-09-20T10:02:00Z"),
    (2, "alan@example.test", "export_report", "2026-09-21T14:30:00Z"),
    (3, "grace@example.test", "delete_order", "2026-09-22T09:15:00Z"),
])
c.commit(); c.close()
print(f"  database  {db}")
for t in ("users", "orders", "audit_log"):
    n = sqlite3.connect(db).execute(f"select count(*) from {t}").fetchone()[0]
    print(f"            {t}: {n} rows")
PY

echo "  fixtures  $REPO/testbed/  (.env, .ssh/id_rsa, deploy/credentials — all fake)"

# Point the config at the database wherever it actually ended up.
python3 - "$REPO" "$DB" <<'PY'
import json, sys
from pathlib import Path
repo, db = sys.argv[1], sys.argv[2]
cfg = Path(repo) / "hypothesise.config.json"
data = json.loads(cfg.read_text()) if cfg.exists() else {}
data.setdefault("mcpServers", {})
data["mcpServers"]["fs"] = {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", repo],
}
data["mcpServers"]["db"] = {
    "command": "npx",
    "args": ["-y", "mcp-server-sqlite-npx", db],
}
data["hypothesise"] = {
    "mode": "observe", "failMode": "open", "budgetMs": 50,
    "apiUrl": "http://127.0.0.1:8080", "agentId": "copilot",
}
cfg.write_text(json.dumps(data, indent=2) + "\n")
print(f"  config    {cfg}")
PY

cat <<MSG

Next:
    1. hypothesise api                      (leave running)
    2. reload VS Code, start a Copilot agent chat
    3. work through docs/TEST-RUN.md

Teardown:  rm -rf "$DB_DIR" "$REPO/testbed"
MSG
