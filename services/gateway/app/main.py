"""API gateway / BFF — FastAPI.

Fronts the Next.js portal and routes to the LOS and LSS services. Adds a session-auth
layer: `/auth/*` for login/logout, and a guard on the servicing (`/lss/*`) routes. The
resolved identity is forwarded downstream as `X-User-Id` / `X-User-Role` headers.

NOTE (brownfield): the gateway authenticates the caller but does NOT enforce role
authorization on money-moving servicing actions — that is left to the downstream
servicing-service, which also doesn't check. Any authenticated user can adjust balances
or waive fees. (weak authz — kept on purpose)
"""
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth
from .config import (
    DECISION_URL,
    DISCLOSURE_URL,
    KYC_URL,
    ORIGINATION_URL,
    PAYMENT_URL,
    SERVICING_URL,
)
from .logging_config import get_logger

log = get_logger("gateway")

app = FastAPI(title="Meridian Gateway (BFF)", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # wide-open CORS (brownfield)
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway"}


# --------------------------------------------------------------------------- auth

class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(body: LoginIn):
    try:
        user = auth.authenticate(body.username, body.password)
    except Exception as e:  # DB/redis down
        log.warning("login backend error: %s", e)
        raise HTTPException(status_code=503, detail="auth backend unavailable")
    if not user:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = auth.create_session(user)
    return {"token": token, "user": user}


@app.post("/auth/logout")
def logout(authorization: str | None = Header(None)):
    auth.delete_session(auth.bearer_token(authorization))
    return {"ok": True}


@app.get("/auth/me")
def me(authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


# -------------------------------------------------------------------------- proxy

async def _proxy(base: str, path: str, request: Request, user: dict | None):
    method = request.method
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    }
    if user:
        headers["X-User-Id"] = str(user.get("id", ""))
        headers["X-User-Role"] = str(user.get("role", ""))
    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.request(
            method, f"{base}{path}", content=body, headers=headers,
            params=request.query_params,
        )
    try:
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception:
        return JSONResponse(status_code=resp.status_code, content={"raw": resp.text})


def _require_user(authorization: str | None) -> dict:
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


@app.api_route("/los/{path:path}", methods=["GET", "POST"])
async def los(path: str, request: Request, authorization: str | None = Header(None)):
    # Origination is borrower-facing; an applicant can apply without an account.
    # If a session is present we forward it, otherwise we proxy anonymously.
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(ORIGINATION_URL, f"/{path}", request, user)


@app.api_route("/lss/{path:path}", methods=["GET", "POST"])
async def lss(path: str, request: Request, authorization: str | None = Header(None)):
    # Servicing requires authentication (but not a specific role — see module docstring).
    user = _require_user(authorization)
    return await _proxy(SERVICING_URL, f"/{path}", request, user)


# --- LOS sub-services (the decomposed origination estate). -------------------
# Origination calls these server-to-server during the application flow; they are
# also exposed here so the portal / ops tooling can reach each service directly.
# Like /los/*, the underwriting-flow services forward a session if one is present
# but do not require it (an applicant can apply without an account).

@app.api_route("/kyc/{path:path}", methods=["GET", "POST"])
async def kyc(path: str, request: Request, authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(KYC_URL, f"/{path}", request, user)


@app.api_route("/decision/{path:path}", methods=["GET", "POST"])
async def decision(path: str, request: Request, authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(DECISION_URL, f"/{path}", request, user)


@app.api_route("/disclosure/{path:path}", methods=["GET", "POST"])
async def disclosure(path: str, request: Request, authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(DISCLOSURE_URL, f"/{path}", request, user)


@app.api_route("/payments/{path:path}", methods=["GET", "POST"])
async def payments(path: str, request: Request, authorization: str | None = Header(None)):
    # Taking a payment is a money-moving action: authenticated, but (brownfield)
    # the gateway still does NOT enforce a specific role — same gap as /lss.
    user = _require_user(authorization)
    return await _proxy(PAYMENT_URL, f"/{path}", request, user)
