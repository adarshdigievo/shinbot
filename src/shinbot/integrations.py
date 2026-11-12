"""Bounded HTTP clients for native integrations with credential-safe failures."""

import httpx


async def request_json(client, method, url, **kwargs):
    try:
        async with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                code = response.status_code
                message = (
                    "Account authorization expired or lacks permission. Reconnect in settings."
                    if code in {401, 403}
                    else "The service is rate limited. Try again later."
                    if code == 429
                    else "This resource is unavailable to the connected account."
                    if code == 404
                    else "The service rejected the request. Check the connection settings."
                )
                raise ValueError(message)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 4_000_000:
                    raise ValueError(
                        "The response is too large. Narrow the selected repositories or date range."
                    )
            import json

            try:
                return json.loads(body), dict(response.headers)
            except (ValueError, UnicodeDecodeError):
                raise ValueError("The service returned an invalid response. Try again later.") from None
    except httpx.HTTPError:
        raise ValueError("The service could not be reached. Check your connection and try again.") from None


def client():
    return httpx.AsyncClient(timeout=25, trust_env=False, follow_redirects=False)
