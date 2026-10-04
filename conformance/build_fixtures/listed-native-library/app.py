import os

from flask import Flask, request
from pdf2image import convert_from_bytes

app = Flask(__name__)


@app.post("/pages")
def pages():
    return {"pages": len(convert_from_bytes(request.files["pdf"].read()))}


@app.get("/")
def index():
    return "<h1>Upload a PDF to /pages</h1>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
