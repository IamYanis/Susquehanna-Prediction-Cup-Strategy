"""Public Polymarket metadata/books only; no trading SDK or credentials needed."""
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 10
REQUEST_SPACING = 0.20
MAX_RETRY_WAIT = 5
# Research policy only. The existing SIG/live freshness policies are unchanged.
MAX_BOOK_AGE = 60
MAX_RECEIVE_AGE = 5


class ExternalDataError(ValueError):
    """Fixed, credential-free failure reason suitable for a research report."""


def require(condition, reason):
    if not condition:
        raise ExternalDataError(reason)


class ReadOnlySession(requests.Session):
    """Restrict requests to the handful of GET routes used by this scanner.

    Separate sessions prevent the local SIG bearer token from reaching a public
    Polymarket endpoint. Reject redirects rather than following another host.
    """
    def __init__(self, venue):
        super().__init__()
        require(venue in ("SIG", "POLYMARKET"), "INVALID_VENUE")
        self.venue = venue
        self.last_read_started = None
        if venue == "POLYMARKET":
            self.trust_env = False  # In particular, never load a .netrc login.

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        require(method.upper() == "GET" and parsed.scheme == "https"
                and not parsed.username and not parsed.password and not parsed.port,
                "READ_ONLY_ROUTE_REQUIRED")
        if self.venue == "POLYMARKET":
            allowed = ((parsed.netloc == "gamma-api.polymarket.com"
                        and re.fullmatch(r"/markets/[1-9][0-9]*", parsed.path))
                       or (parsed.netloc == "clob.polymarket.com" and parsed.path == "/book"))
            headers = dict(self.headers)
            headers.update(kwargs.get("headers") or {})
            require(self.auth is None and kwargs.get("auth") is None
                    and not any(k.lower() in ("authorization", "proxy-authorization", "cookie") for k in headers)
                    and not self.cookies and not kwargs.get("cookies"), "PUBLIC_CLIENT_MUST_NOT_HAVE_CREDENTIALS")
        else:
            allowed = (parsed.netloc == "sig.thesuper.market"
                       and re.fullmatch(r"/api/v1/(tournaments/[a-z0-9-]+|markets/[1-9][0-9]*(/nodes)?|exchanges/[1-9][0-9]*/orderbook)", parsed.path))
        require(bool(allowed) and kwargs.get("allow_redirects", False) is False, "READ_ONLY_ROUTE_REQUIRED")
        kwargs["allow_redirects"] = False
        try:
            return super().request(method, url, **kwargs)
        finally:
            if self.venue == "POLYMARKET":
                # Gamma sets an anonymous Cloudflare cookie even on public GETs.
                # Discard it, so the next request stays credential/cookie free.
                self.cookies.clear()


def number(value, probability=False):
    require(not isinstance(value, bool) and value is not None, "INVALID_EXTERNAL_NUMBER")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExternalDataError("INVALID_EXTERNAL_NUMBER") from None
    require(result.is_finite() and result >= 0 and (not probability or result <= 1), "INVALID_EXTERNAL_NUMBER")
    return result


def array(value):
    """Gamma historically encodes arrays as JSON strings; accept actual arrays too."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ExternalDataError("INVALID_EXTERNAL_ARRAY") from None
    require(isinstance(value, list), "INVALID_EXTERNAL_ARRAY")
    return value


def fetch_json(session, url, params=None):
    """Bounded retries apply only to GETs. Long rate-limit waits end this scan."""
    for attempt in range(MAX_ATTEMPTS):
        previous = getattr(session, "last_read_started", None)
        if previous is not None:
            time.sleep(max(0, REQUEST_SPACING - (time.monotonic() - previous)))
        session.last_read_started = time.monotonic()
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError):
            if attempt + 1 == MAX_ATTEMPTS:
                raise ExternalDataError("EXTERNAL_API_UNAVAILABLE") from None
            time.sleep(2 ** attempt)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            delay = 2 ** attempt
            if response.status_code == 429:
                header = response.headers.get("Retry-After", "1")
                try:
                    delay = float(header)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(header).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = MAX_RETRY_WAIT + 1
                require(0 <= delay <= MAX_RETRY_WAIT, "EXTERNAL_RATE_LIMITED")
            if attempt + 1 == MAX_ATTEMPTS:
                raise ExternalDataError("EXTERNAL_RATE_LIMITED" if response.status_code == 429 else "EXTERNAL_API_UNAVAILABLE")
            time.sleep(delay)
            continue
        require(response.status_code == 200, "EXTERNAL_API_RESPONSE_UNAVAILABLE")
        try:
            payload = response.json()
        except ValueError:
            raise ExternalDataError("INVALID_EXTERNAL_JSON") from None
        require(isinstance(payload, dict), "INVALID_EXTERNAL_RESPONSE")
        return payload


def read_market(session, market_id):
    require(re.fullmatch(r"[1-9][0-9]*", str(market_id)) is not None, "INVALID_EXTERNAL_MARKET_ID")
    raw = fetch_json(session, f"{GAMMA_URL}/markets/{market_id}")
    require(str(raw.get("id")) == str(market_id), "EXTERNAL_MARKET_ID_MISMATCH")
    outcomes, tokens = array(raw.get("outcomes")), array(raw.get("clobTokenIds"))
    require(len(outcomes) == len(tokens) == 2 and set(outcomes) == {"Yes", "No"}
            and len(set(tokens)) == 2 and all(isinstance(t, str) and re.fullmatch(r"[1-9][0-9]*", t) for t in tokens),
            "UNSUPPORTED_EXTERNAL_OUTCOMES")
    require(isinstance(raw.get("conditionId"), str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", raw["conditionId"]),
            "INVALID_EXTERNAL_CONDITION_ID")
    # Protocol V2 uses position IDs instead of these CTF token IDs. Never guess.
    require(raw.get("version") in (None, "v1"), "EXTERNAL_PROTOCOL_NEEDS_REVALIDATION")
    prices = raw.get("outcomePrices")
    prices = array(prices) if prices is not None else [None, None]
    require(len(prices) == 2, "INVALID_EXTERNAL_REFERENCE_PRICES")
    return {"raw": raw, "market_id": str(market_id), "condition_id": raw["conditionId"],
            "tokens": dict(zip(outcomes, tokens)),
            "headline": {side: number(p, True) if p is not None else None for side, p in zip(outcomes, prices)},
            "liquidity": number(raw["liquidity"]) if raw.get("liquidity") is not None else None,
            "volume": number(raw["volume"]) if raw.get("volume") is not None else None,
            "open": raw.get("active") is True and raw.get("closed") is False
                    and raw.get("enableOrderBook") is True and raw.get("acceptingOrders") is True}


def read_book(session, token_id, condition_id):
    raw = fetch_json(session, f"{CLOB_URL}/book", {"token_id": token_id})
    return normalize_book(raw, token_id, condition_id, time.time())


def normalize_book(raw, token_id, condition_id, fetched_at):
    require(raw.get("asset_id") == token_id and raw.get("market") == condition_id, "EXTERNAL_BOOK_ID_MISMATCH")
    result = {"fetched_at": fetched_at, "book_at": None, "bid": None, "ask": None,
              "bid_depth": Decimal(0), "ask_depth": Decimal(0)}
    if raw.get("timestamp") is not None:
        result["book_at"] = float(number(raw["timestamp"]) / 1000)  # Official epoch milliseconds.
        require(result["book_at"] > 0, "INVALID_EXTERNAL_BOOK_TIMESTAMP")
    for side, best in (("bids", "bid"), ("asks", "ask")):
        require(isinstance(raw.get(side), list), "INVALID_EXTERNAL_BOOK_LEVELS")
        levels = [(number(l["price"], True), number(l["size"])) for l in raw[side]]
        levels = [(p, size) for p, size in levels if size > 0]
        if levels:
            # CLOB arrays are not best-first. Scan the complete supplied book.
            price = (max if best == "bid" else min)(p for p, _ in levels)
            result[best] = price
            result[best + "_depth"] = sum((size for p, size in levels if p == price), Decimal(0))
    require(result["bid"] is None or result["ask"] is None or result["bid"] <= result["ask"], "CROSSED_EXTERNAL_BOOK")
    result["midpoint"] = ((result["bid"] + result["ask"]) / 2
                          if result["bid"] is not None and result["ask"] is not None else None)
    result["spread"] = (result["ask"] - result["bid"] if result["midpoint"] is not None else None)
    return result


def is_fresh(book, now):
    return (book is not None and book["book_at"] is not None
            and -5 <= now - book["book_at"] <= MAX_BOOK_AGE
            and 0 <= now - book["fetched_at"] <= MAX_RECEIVE_AGE)


def reference(book, headline):
    """Only a two-sided book has high data quality; fallback is visibly labelled."""
    if book and book["midpoint"] is not None:
        return book["midpoint"], "BOOK_MIDPOINT", "HIGH"
    if book and book["ask"] is not None:
        return book["ask"], "ONE_SIDED_ASK", "LOW"
    if book and book["bid"] is not None:
        return book["bid"], "ONE_SIDED_BID", "LOW"
    return headline, "HEADLINE_REFERENCE_ONLY", "LOW"
