import os

import pytesseract
from flask import Flask, request
from PIL import Image

app = Flask(__name__)


@app.post("/read")
def read():
    return pytesseract.image_to_string(Image.open(request.files["image"].stream))


@app.get("/")
def index():
    return "<h1>Upload a scan to /read</h1>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
