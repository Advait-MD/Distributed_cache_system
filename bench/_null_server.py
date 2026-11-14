"""
Minimal ASGI app used ONLY to measure the load generator's own ceiling.

    taskset -c 0-2 python -m uvicorn bench._null_server:app --port 9199 \
        --no-access-log --log-level warning

If the httpx client can push far more req/s here than through the
router, the router (not the client) is the bottleneck.
"""

BODY = b'{"key":"k","value":1,"source":"cache","node":"n"}'


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": BODY})
