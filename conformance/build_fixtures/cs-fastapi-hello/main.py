from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"tool": "invoice checker"}

@app.get("/invoices")
def invoices():
    return [{"id": 1, "amount": 120.5}]

@app.get("/invoices/{invoice_id}")
def invoice(invoice_id: int):
    return {"id": invoice_id, "amount": 120.5}
