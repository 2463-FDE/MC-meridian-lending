"""API gateway / BFF — FastAPI.

Fronts the Next.js portal and routes to the LOS and LSS services. Adds a session-auth
layer: `/auth/*` for login/logout, and a guard on the servicing (`/lss/*`) routes. The
resolved identity is forwarded downstream as `X-User-Id` / `X-User-Role` headers.

NOTE (brownfield): the gateway authenticates the caller but does NOT enforce role
authorization on money-moving servicing actions — that is left to the downstream
servicing-service, which also doesn't check. Any authenticated user can adjust balances
or waive fees. (weak authz — kept on purpose)
"""

import asyncio

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import auth, config
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

# Credentialed CORS (PR #7 review): the anonymous resume cookie is sent with
# credentials:"include", and the CORS spec forbids pairing that with a "*" origin, so the
# gateway must name the concrete portal origin and allow credentials. Non-browser callers
# (ops tooling, service-to-service) are unaffected — CORS is a browser-only control.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.PORTAL_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Resume cookie: HttpOnly (no JS read), SameSite=Strict, Path=/los (only sent to LOS
# routes). Holds an opaque Redis session id, never the continuation token itself.
RESUME_COOKIE = "meridian_resume"
RESUME_COOKIE_PATH = "/los"


@app.get("/health")
def health():
    missing = config.missing_required_secrets()
    if missing:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "gateway",
                "missing_secrets": missing,
            },
        )
    ok, db_error = config.database_reachable()
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "gateway",
                "database_error": db_error,
            },
        )
    # Auth/session flows live in Redis, so a Redis outage must fail readiness too —
    # otherwise the load balancer keeps sending login/session traffic to an instance
    # that cannot authenticate.
    redis_ok, redis_error = config.redis_reachable()
    if not redis_ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "gateway",
                "redis_error": redis_error,
            },
        )
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


async def _proxy_raw(
    base: str,
    path: str,
    request: Request,
    user: dict | None,
    inject_token: str | None = None,
):
    """Forward the request downstream and return (status_code, content).

    inject_token, when set, is attached as X-Application-Token — this is the ONLY source of
    that header (a client-supplied copy is always stripped, like X-User-*), so the anonymous
    resume capability comes from the gateway's server-side session, never the browser."""
    method = request.method
    body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        # Strip trust headers a client might send: downstream services trust
        # X-User-* / X-Internal-Service as identity, and X-Application-Token as the
        # anonymous capability, so an external caller must never inject them through the
        # proxy. The gateway is their only source — X-User-* set below from the session,
        # X-Application-Token injected below from the resume session, X-Internal-Service
        # only on internal service-to-service calls (never here). (PR review)
        if k.lower()
        not in (
            "host",
            "content-length",
            "authorization",
            "x-user-id",
            "x-user-role",
            "x-internal-service",
            "x-application-token",
        )
    }
    if user:
        headers["X-User-Id"] = str(user.get("id", ""))
        headers["X-User-Role"] = str(user.get("role", ""))
    if inject_token:
        headers["X-Application-Token"] = inject_token
    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.request(
            method,
            f"{base}{path}",
            content=body,
            headers=headers,
            params=request.query_params,
        )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


async def _proxy(base: str, path: str, request: Request, user: dict | None):
    status, content = await _proxy_raw(base, path, request, user)
    return JSONResponse(status_code=status, content=content)


def _require_user(authorization: str | None) -> dict:
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def _app_id_in_path(path: str) -> str | None:
    """The application id in an application-scoped LOS path (applications/{id}/...), or None
    for the collection route (applications) and non-application paths (offer)."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "applications" and parts[1].isdigit():
        return parts[1]
    return None


async def _create_resume_session_with_retry(app_id, token, attempts: int = 3):
    """Create the resume session, riding a transient Redis blip with a short bounded retry.
    Returns the session id, or None if Redis is still unreachable after all attempts (the
    caller then fails the submit closed rather than 500-ing with the raw token discarded)."""
    for i in range(attempts):
        try:
            return auth.create_resume_session(app_id, token)
        except Exception as e:  # noqa: BLE001 -- any Redis error is a retryable outage here
            log.warning(
                "resume session create failed (attempt %d/%d) app_id=%s: %s",
                i + 1,
                attempts,
                app_id,
                type(e).__name__,
            )
            if i < attempts - 1:
                await asyncio.sleep(0.05 * (i + 1))
    return None


def _resume_unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "resume session store unavailable; please retry"},
    )


def _set_resume_cookie(response: JSONResponse, sid: str) -> None:
    response.set_cookie(
        RESUME_COOKIE,
        sid,
        max_age=config.RESUME_TTL_SECONDS,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="strict",
        path=RESUME_COOKIE_PATH,
    )


@app.api_route("/los/{path:path}", methods=["GET", "POST"])
async def los(path: str, request: Request, authorization: str | None = Header(None)):
    # Origination is borrower-facing; an applicant can apply without an account.
    # If a session is present we forward it, otherwise we proxy anonymously.
    user = auth.get_session(auth.bearer_token(authorization))

    # Submit atomicity (PR #7 review). Submit commits an application whose ONLY anonymous
    # credential is the resume session stored here. If Redis is down we must NOT let
    # origination create that application and then discard the one-time raw token when the
    # session write fails -- that strands the applicant (500, no cookie, no recovery). So for
    # an anonymous submit, refuse up front when Redis is unreachable: the applicant retries
    # cleanly with no orphaned row. (An authenticated owner needs no resume session.) The
    # residual post-check race is handled by a bounded retry + controlled 503 below.
    is_submit_attempt = (
        request.method == "POST" and path.strip("/") == "applications" and user is None
    )
    if is_submit_attempt and not config.redis_reachable()[0]:
        return _resume_unavailable()

    # Anonymous resume capability (ADR 0010 Phase B, PR #7 review). The continuation token
    # lives server-side in Redis, keyed by the opaque id in the HttpOnly resume cookie. On
    # an application-scoped request we resolve the cookie and inject the token downstream
    # ONLY when it belongs to the app id in the path (a cookie for app A cannot authorize
    # app B). A logged-in owner/officer needs no cookie; their session authorizes them.
    sid = request.cookies.get(RESUME_COOKIE)
    inject_token = None
    app_id = _app_id_in_path(path)
    sess = None
    if sid:
        try:
            sess = auth.resolve_resume(sid)
        except Exception as e:  # noqa: BLE001 -- Redis outage on the resume read path
            # Parity with the submit path (PR #7 review): a Redis blip must not 500 a resume
            # request. An anonymous caller's capability lives in Redis, so surface a retryable
            # 503; an authenticated caller doesn't need it -- they proceed via their session.
            log.warning("resume session resolve failed: %s", type(e).__name__)
            if user is None:
                return _resume_unavailable()
            sess = None
    if sess:
        if app_id is not None and str(sess.get("app_id")) == app_id:
            # Application-scoped path (applications/{id}/...): inject only when the cookie
            # belongs to that id -- a cookie for app A never drives app B.
            inject_token = sess.get("token")
        elif path.strip("/") == "offer":
            # /los/offer carries app_id in the BODY, not the path. Inject the session token;
            # origination binds the offer to body.app_id and validates the token against THAT
            # application (a mismatched app 404s there), so scope is enforced downstream.
            inject_token = sess.get("token")

    status, content = await _proxy_raw(
        ORIGINATION_URL, f"/{path}", request, user, inject_token=inject_token
    )

    # The raw continuation token must NEVER reach the browser. Origination returns it in the
    # submit response (freshly issued) and echoes it in the recheck-kyc response, so strip it
    # from EVERY LOS response body before returning; custody is the HttpOnly cookie + the
    # server-side session, not client JS.
    raw_token = content.get("continuation_token") if isinstance(content, dict) else None
    app_id_in_body = content.get("app_id") if isinstance(content, dict) else None
    if raw_token:
        content = {k: v for k, v in content.items() if k != "continuation_token"}

    response = JSONResponse(status_code=status, content=content)

    # Submit: capture the freshly issued token into a server-side resume session and hand the
    # browser only the HttpOnly cookie holding its opaque id. ANONYMOUS submits only -- an
    # authenticated owner resumes via their login session (owner authz) and needs no resume
    # cookie, so we neither create a session for them nor 503 their submit on a Redis outage.
    is_submit = (
        request.method == "POST"
        and path.strip("/") == "applications"
        and status == 200
        and user is None
        and raw_token
        and app_id_in_body is not None
    )
    if is_submit:
        new_sid = await _create_resume_session_with_retry(app_id_in_body, raw_token)
        if new_sid is None:
            # Redis went down in the window after the pre-check and stayed down through the
            # retry. The application is committed but has no resume session; it is inert (no
            # decision/offer/loan) and officer-reconcilable. Return a retryable 503 rather
            # than a 500 that silently discards the raw token.
            log.error(
                "resume session unstorable after submit for app_id=%s; application is inert "
                "(no decision/offer/loan), officer-reconcilable",
                app_id_in_body,
            )
            return _resume_unavailable()
        _set_resume_cookie(response, new_sid)
        return response

    # Accept boards the loan (terminal money action): revoke the resume session server-side
    # and clear the cookie so the capability cannot be replayed after funding.
    is_accept = (
        request.method == "POST"
        and app_id is not None
        and path.strip("/").endswith(f"applications/{app_id}/accept")
        and status == 200
    )
    if is_accept and sid:
        auth.clear_resume(sid)
        response.delete_cookie(RESUME_COOKIE, path=RESUME_COOKIE_PATH)

    return response


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
async def decision(
    path: str, request: Request, authorization: str | None = Header(None)
):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(DECISION_URL, f"/{path}", request, user)


@app.api_route("/disclosure/{path:path}", methods=["GET", "POST"])
async def disclosure(
    path: str, request: Request, authorization: str | None = Header(None)
):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(DISCLOSURE_URL, f"/{path}", request, user)


@app.api_route("/payments/{path:path}", methods=["GET", "POST"])
async def payments(
    path: str, request: Request, authorization: str | None = Header(None)
):
    # Taking a payment is a money-moving action: authenticated, but (brownfield)
    # the gateway still does NOT enforce a specific role — same gap as /lss.
    user = _require_user(authorization)
    return await _proxy(PAYMENT_URL, f"/{path}", request, user)
