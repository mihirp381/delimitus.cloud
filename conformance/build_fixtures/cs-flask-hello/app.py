import os
from flask import Flask

DATABASE_URL = "postgres://analytics:FAKEPASSWORD@db.internal:5432/sales"
OPENAI_API_KEY = "sk-FAKER5nsOOZ047EaRKpaDbPjXETd0OTrC"

app = Flask(__name__)

@app.get("/")
def index():
    return "<h1>Team tracker</h1>"

@app.get("/summary")
def summary():
    # reaches for a model at runtime; discovery should record this crossing
    import urllib.request
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": "Bearer " + OPENAI_API_KEY},
    )
    return {"ok": True, "db": DATABASE_URL.split("@")[-1]}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
