#!/usr/bin/env python3
"""
TLS -> SGN flight search via Amadeus Self-Service API (read-only).

Endpoints used (all GET, no writes, no booking):
  POST /v1/security/oauth2/token        (client_credentials)
  GET  /v2/shopping/flight-offers

Setup:
  1. Create a free account at https://developers.amadeus.com/
  2. Create a Self-Service app, copy the API Key and API Secret.
  3. export AMADEUS_API_KEY=...
     export AMADEUS_API_SECRET=...
  4. pip install -r scripts/requirements-flight.txt
  5. python scripts/flight_search.py            # test env (default)
     python scripts/flight_search.py --prod     # production env (real prices)

Defaults: 3 adults, ECONOMY, max 1 stop, depart 2026-12-15 +/-3d,
return 2027-01-07 +/-3d, top 15 cheapest offers (deduped) printed.
Use --help to override.

Note on environments:
  - TEST returns synthetic data with limited carriers/routes.
  - PROD requires moving the app to production in Amadeus's portal
    (free, but they ask a couple of questions). Use --prod once enabled.
"""

import argparse
import itertools
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests


TEST_BASE = "https://test.api.amadeus.com"
PROD_BASE = "https://api.amadeus.com"


def get_token(base, key, secret):
    resp = requests.post(
        f"{base}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": key,
            "client_secret": secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_one(base, token, origin, dest, depart, ret, adults, max_results):
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": dest,
        "departureDate": depart.isoformat(),
        "returnDate": ret.isoformat(),
        "adults": adults,
        "travelClass": "ECONOMY",
        "currencyCode": "EUR",
        "nonStop": "false",
        "max": max_results,
    }
    for attempt in range(3):
        resp = requests.get(
            f"{base}/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if 400 <= resp.status_code < 500:
            return []
        resp.raise_for_status()
        return resp.json().get("data", [])
    return []


def parse_duration(s):
    """Parse ISO-8601 duration like 'PT15H30M' into a timedelta."""
    s = s[2:]
    h = m = 0
    if "H" in s:
        head, s = s.split("H", 1)
        h = int(head)
    if "M" in s:
        head, _ = s.split("M", 1)
        m = int(head)
    return timedelta(hours=h, minutes=m)


def fmt_dur(td):
    total = int(td.total_seconds() // 60)
    return f"{total // 60}h{total % 60:02d}"


def itin_summary(itin):
    segs = itin["segments"]
    route = segs[0]["departure"]["iataCode"]
    for s in segs:
        route += "->" + s["arrival"]["iataCode"]
    return {
        "stops": len(segs) - 1,
        "duration": parse_duration(itin["duration"]),
        "flights": [f"{s['carrierCode']}{s['number']}" for s in segs],
        "route": route,
        "depart_at": datetime.fromisoformat(segs[0]["departure"]["at"]),
        "arrive_at": datetime.fromisoformat(segs[-1]["arrival"]["at"]),
        "layovers": [segs[i]["arrival"]["iataCode"] for i in range(len(segs) - 1)],
        "aircraft": [s.get("aircraft", {}).get("code", "") for s in segs],
    }


def main():
    p = argparse.ArgumentParser(
        description="Search TLS-SGN flights on Amadeus (read-only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--origin", default="TLS")
    p.add_argument("--destination", default="SGN")
    p.add_argument("--depart", default="2026-12-15", help="Center depart date YYYY-MM-DD")
    p.add_argument("--return", dest="ret", default="2027-01-07", help="Center return date YYYY-MM-DD")
    p.add_argument("--flex", type=int, default=3, help="+/- days around each center date")
    p.add_argument("--adults", type=int, default=3)
    p.add_argument("--max-stops", type=int, default=1)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--per-date", type=int, default=5, help="Offers fetched per date pair")
    p.add_argument("--prod", action="store_true", help="Use production API (default: test)")
    p.add_argument("--sort", choices=["price", "duration", "score"], default="score",
                   help="score = (price/min_price) + (duration/min_duration)")
    args = p.parse_args()

    key = os.environ.get("AMADEUS_API_KEY")
    secret = os.environ.get("AMADEUS_API_SECRET")
    if not key or not secret:
        sys.exit("Set AMADEUS_API_KEY and AMADEUS_API_SECRET env vars.")

    base = PROD_BASE if args.prod else TEST_BASE
    center_dep = date.fromisoformat(args.depart)
    center_ret = date.fromisoformat(args.ret)
    flex = args.flex
    depart_dates = [center_dep + timedelta(days=d) for d in range(-flex, flex + 1)]
    return_dates = [center_ret + timedelta(days=d) for d in range(-flex, flex + 1)]
    combos = [(d, r) for d, r in itertools.product(depart_dates, return_dates) if r > d]

    env = "PROD" if args.prod else "TEST"
    print(f"# Amadeus flight search ({env} env)")
    print(f"# {args.origin} -> {args.destination} | {args.adults} adults | ECONOMY"
          f" | max {args.max_stops} stop(s)")
    print(f"# Depart {center_dep} +/-{flex}d, Return {center_ret} +/-{flex}d"
          f" => {len(combos)} date combos")
    print()

    token = get_token(base, key, secret)

    all_offers = []
    for i, (d, r) in enumerate(combos, 1):
        sys.stderr.write(f"\r  querying {i}/{len(combos)} ({d} -> {r})...   ")
        sys.stderr.flush()
        offers = search_one(base, token, args.origin, args.destination,
                            d, r, args.adults, args.per_date)
        for o in offers:
            o["_dep"], o["_ret"] = d, r
        all_offers.extend(offers)
        time.sleep(0.12)
    sys.stderr.write("\n\n")

    filtered = [
        o for o in all_offers
        if all((len(it["segments"]) - 1) <= args.max_stops for it in o["itineraries"])
    ]

    if not filtered:
        print("No offers matched. Try --max-stops 2, widen --flex, or check credentials.")
        return

    # dedupe by exact flight numbers (keep cheapest)
    seen = {}
    for o in filtered:
        key_ = tuple(
            tuple(s["carrierCode"] + s["number"] for s in it["segments"])
            for it in o["itineraries"]
        )
        if key_ not in seen or float(o["price"]["grandTotal"]) < float(seen[key_]["price"]["grandTotal"]):
            seen[key_] = o
    uniq = list(seen.values())

    min_price = min(float(o["price"]["grandTotal"]) for o in uniq)
    min_dur = min(
        sum((parse_duration(it["duration"]) for it in o["itineraries"]), timedelta()).total_seconds()
        for o in uniq
    )

    def total_dur(o):
        return sum((parse_duration(it["duration"]) for it in o["itineraries"]), timedelta()).total_seconds()

    if args.sort == "price":
        uniq.sort(key=lambda o: float(o["price"]["grandTotal"]))
    elif args.sort == "duration":
        uniq.sort(key=total_dur)
    else:
        uniq.sort(key=lambda o: float(o["price"]["grandTotal"]) / min_price + total_dur(o) / min_dur)

    top = uniq[: args.top]
    print(f"## Top {len(top)} offers (sorted by {args.sort}, deduped on flight numbers)\n")
    for rank, o in enumerate(top, 1):
        price = float(o["price"]["grandTotal"])
        currency = o["price"]["currency"]
        per_pax = price / args.adults
        out = itin_summary(o["itineraries"][0])
        ret = itin_summary(o["itineraries"][1]) if len(o["itineraries"]) > 1 else None
        carriers = sorted({s["carrierCode"] for it in o["itineraries"] for s in it["segments"]})
        bag = o["travelerPricings"][0]["fareDetailsBySegment"][0].get("includedCheckedBags", {})
        bag_str = (
            f"{bag.get('weight')}{bag.get('weightUnit','KG')}" if bag.get("weight")
            else f"{bag.get('quantity', 0)}pc" if bag.get("quantity") is not None else "n/a"
        )
        score = price / min_price + total_dur(o) / min_dur

        print(f"### #{rank}  {price:.0f} {currency} total  ({per_pax:.0f} {currency}/pax)"
              f"  -  {'/'.join(carriers)}  -  score {score:.2f}")
        print(f"  Offer ID: {o['id']}   Bag: {bag_str}   Seats left: "
              f"{o.get('numberOfBookableSeats', '?')}")
        print(f"  OUT  {o['_dep']}  {out['route']}  {fmt_dur(out['duration'])}"
              f"  {out['stops']}stop  flights: {' + '.join(out['flights'])}")
        print(f"       {out['depart_at']:%a %d/%m %H:%M} -> {out['arrive_at']:%a %d/%m %H:%M}"
              + (f"  via {'/'.join(out['layovers'])}" if out['layovers'] else ""))
        if ret:
            print(f"  RET  {o['_ret']}  {ret['route']}  {fmt_dur(ret['duration'])}"
                  f"  {ret['stops']}stop  flights: {' + '.join(ret['flights'])}")
            print(f"       {ret['depart_at']:%a %d/%m %H:%M} -> {ret['arrive_at']:%a %d/%m %H:%M}"
                  + (f"  via {'/'.join(ret['layovers'])}" if ret['layovers'] else ""))
        print()


if __name__ == "__main__":
    main()
