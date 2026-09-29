from flask import Flask

app = Flask(__name__)


@app.get("/")
def index() -> str:
    return "ok"


if __name__ == "__main__":
    app.run(port=5000)
