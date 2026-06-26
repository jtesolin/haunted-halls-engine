from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import health

app = FastAPI(
    title="Haunted Halls API",
    version="0.1.0",
)

# For local Next.js development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Haunted Halls API"}