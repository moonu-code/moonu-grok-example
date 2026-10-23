#!/usr/bin/env python3
"""Send DTMF to an already answered Moonu call. Never register, answer or hang up."""
import argparse
import asyncio
import getpass
import os
import re

import aiohttp

from .client import Client, ClientError, validate_url
from .logs import log


async def send_digits(client, cid, digits, duration_ms=160, gap_ms=100):
    if not re.fullmatch(r"[0-9A-D*#]{1,32}", digits):
        raise ClientError("Use 1–32 DTMF digits: 0–9, A–D, * or #")
    path = f"/registrations/{client.rid}"
    # Start from the current cursor; do not treat a historical result as success.
    cursor = "0-0"
    while True:
        replay = await client.rest("GET", path + "/events?after=" + cursor)
        previous, cursor = cursor, replay["cursor"]
        if previous == cursor or len(replay["items"]) < 100:
            break
    for index, digit in enumerate(digits, 1):
        # No retry on transport failure: the server may already have submitted it.
        result = await client.rest("POST", path + f"/calls/{cid}/dtmf",
                                   {"digit": digit, "durationMs": duration_ms}, command=True)
        request_id = result["requestId"]
        async with asyncio.timeout(12):
            while True:
                events = await client.rest("GET", path + "/events?after=" + cursor)
                cursor = events["cursor"]
                matching = [e for e in events["items"] if e.get("callId") == cid
                            and e.get("requestId") == request_id and e.get("type") in ("dtmf.submitted", "dtmf.failed")]
                if matching:
                    if matching[0]["type"] != "dtmf.submitted":
                        raise ClientError("DTMF submission unconfirmed; stopped without retry")
                    break
                await asyncio.sleep(.05)
        log(f"DTMF {index}/{len(digits)} submitted; transfer not confirmed.", flush=True)
        await asyncio.sleep((duration_ms + gap_ms) / 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-url", default="http://localhost:8080")
    parser.add_argument("--registration-id", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--digits", required=True)
    parser.add_argument("--duration-ms", type=int, default=160)
    parser.add_argument("--gap-ms", type=int, default=100)
    parser.add_argument("--allow-insecure", action="store_true")
    args = parser.parse_args()
    try:
        if not all(re.fullmatch("[a-f0-9]{32}", v) for v in (args.registration_id, args.call_id)):
            raise ClientError("Invalid registration/call ID")
        if not 80 <= args.duration_ms <= 500 or not 70 <= args.gap_ms <= 5000:
            raise ClientError("Duration must be 80–500 ms and gap 70–5000 ms")
        args.portal_url = validate_url(args.portal_url, ("http", "https"), args.allow_insecure)
        token = os.environ.get("MOONU_API_TOKEN") or getpass.getpass("Portal API Token (hidden): ")
        if not token or any(c.isspace() for c in token):
            raise ClientError("Invalid credential input")
        async def run():
            async with aiohttp.ClientSession() as session:
                client = Client(session, args, token)
                client.rid = args.registration_id
                await send_digits(client, args.call_id, args.digits, args.duration_ms, args.gap_ms)
        asyncio.run(run())
    except KeyboardInterrupt:
        log("Stopped; call and registration unchanged by this client.")
    except Exception as error:
        log(str(error) if isinstance(error, ClientError) else "DTMF unconfirmed; no automatic retry. Check call/network.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
