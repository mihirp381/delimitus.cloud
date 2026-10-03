import os
import sqlite3

from flask import Flask

app = Flask(__name__)
db = sqlite3.connect("data.db", check_same_thread=False)


@app.get("/")
def index() -> str:
    return str(db.execute("select 1").fetchone()[0])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ["PORT"]))
