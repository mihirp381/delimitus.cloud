from flask import Flask
app = Flask(__name__)

@app.get("/")
def index():
    raise RuntimeError("this service boots, binds, and cannot serve")

@app.get("/health")
def health():
    raise RuntimeError("even health is broken, deliberately")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
