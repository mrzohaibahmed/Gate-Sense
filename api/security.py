"""Security headers and the request size limit (pure ASGI middleware, so streaming responses are untouched).

Headers on every HTTP response:
- Content-Security-Policy: only this server's scripts, styles (plus inline style attributes, which React and
  the charts use), images (plus data:/blob: for captured photos), fonts and connections (fetch and the WebSocket);
  no framing, no plugins. The API docs (/docs, /redoc) load Swagger/ReDoc from cdn.jsdelivr.net and get a policy
  that allows it.
- X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy: same-origin,
  Permissions-Policy (camera only for this site: the Enrollment wizard), Cross-Origin-Opener-Policy.
- Strict-Transport-Security when served over HTTPS.

Request bodies larger than MAX_REQUEST_BYTES get 413, whether the client says so (Content-Length) or not
(the body is counted as it arrives).
"""

from app import config

APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "font-src 'self' data:; connect-src 'self'; media-src 'self' blob: mediastream:; object-src 'none'; "
           "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; worker-src blob:; object-src 'none'; "
            "frame-ancestors 'none'")
DOCS_PATHS = ("/docs", "/redoc")


def headers_for(path: str) -> list[tuple[bytes, bytes]]:
    csp = DOCS_CSP if path.startswith(DOCS_PATHS) else APP_CSP
    out = [
        (b"content-security-policy", csp.encode()),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(self), microphone=(), geolocation=(), payment=()"),
        (b"cross-origin-opener-policy", b"same-origin"),
    ]
    if config.HTTPS:
        out.append((b"strict-transport-security", b"max-age=31536000"))
    return out


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = headers_for(scope.get("path", ""))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in extra if k not in names]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class TooLarge(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app, limit: int | None = None):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit or config.MAX_REQUEST_BYTES
        length = dict(scope.get("headers", [])).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            return await self.refuse(send, limit)
        seen = 0
        started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except TooLarge:
            if not started:
                await self.refuse(send, limit)

    @staticmethod
    async def refuse(send, limit: int):
        body = f'{{"detail":"The request is too large (more than {limit // 1_000_000} MB)."}}'.encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
