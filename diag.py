#!/usr/bin/env python3
"""One-off diagnostic: which request shapes still work from this host?"""
import gzip
import http.cookiejar
import json
import os
import re
import urllib.request

EID = os.environ.get("TARGET_ID", "")
API = os.environ.get("TARGET_API_URL", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": '"Chromium";v="126", "Not)A;Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}


def read(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def attempt(label, url, headers, opener=None):
    try:
        req = urllib.request.Request(url, headers=headers)
        op = opener or urllib.request.build_opener()
        with op.open(req, timeout=30) as resp:
            body = read(resp)
            print(f"{label}: HTTP {resp.status}, {len(body)} bytes")
            return body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        print(f"{label}: HTTP {e.code} | {body[:160]!r}")
    except Exception as e:
        print(f"{label}: {type(e).__name__}: {str(e)[:120]}")
    return None


print("=== 1. current approach (plain json + referer)")
attempt("api-plain", API, {
    "User-Agent": UA, "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"https://www.eventbrite.com/checkout-external?eid={EID}",
})

print("\n=== 2. api with full browser headers")
h = dict(BROWSER_HEADERS)
h["Referer"] = f"https://www.eventbrite.com/checkout-external?eid={EID}"
attempt("api-browser", API, h)

print("\n=== 3. warm session: fetch checkout page for cookies, then api")
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
page_h = dict(BROWSER_HEADERS)
page_h.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
               "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1"})
html = attempt("checkout-html", f"https://www.eventbrite.com/checkout-external?eid={EID}",
               page_h, opener)
print(f"   cookies obtained: {len(cj)}")
if html:
    m = re.search(r"window\.__SERVER_DATA__\s*=\s*\{", html)
    print(f"   __SERVER_DATA__ present: {bool(m)}")
attempt("api-warmed", API, h, opener)

print("\n=== 4. public event page (html)")
url = os.environ.get("TARGET_URL", "")
if url:
    body = attempt("event-page", url, page_h, opener)
    if body:
        print(f"   has 'ticketAvailability': {'ticketAvailability' in body}")
        print(f"   has 'salesStatus': {'salesStatus' in body}")

print("\n=== 5. eventbrite public api (no auth)")
attempt("destination-api",
        f"https://www.eventbrite.com/api/3/destination/events/?event_ids={EID}"
        "&expand=ticket_availability", h)

print("\n=== runner egress IP")
try:
    with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=15) as r:
        print(read(r))
except Exception as e:
    print(f"ip lookup failed: {type(e).__name__}")
