from fastapi import FastAPI

app = FastAPI(title="ssc-edge")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
