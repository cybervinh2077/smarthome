from fastapi import FastAPI

app = FastAPI(title="SmartHome Server")


@app.get("/")
def root():
    return {"status": "ok"}
