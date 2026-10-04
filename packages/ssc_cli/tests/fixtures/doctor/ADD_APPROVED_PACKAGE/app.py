import os

import pytesseract
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return pytesseract.get_tesseract_version().public


app.run(host="0.0.0.0", port=int(os.environ["PORT"]))
