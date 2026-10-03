import os
import sqlite3

from flask import Flask

app = Flask(__name__)
db = sqlite3.connect("data/tracker.db", check_same_thread=False)
db.execute("create table if not exists visits (at text)")


@app.get("/")
def index():
    db.execute("insert into visits values (datetime('now'))")
    count = db.execute("select count(*) from visits").fetchone()[0]
    return f"<h1>{count} visits</h1>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
