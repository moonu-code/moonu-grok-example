#!/usr/bin/env python3
"""Standalone public-API audio smoke client. No backend/Janus imports."""
import argparse
import asyncio
import getpass
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import time
from urllib.parse import urlsplit
import uuid
import wave

import aiohttp

from .logs import log

HEADER = struct.Struct("!BBHIII")
RATE, SAMPLES = 24000, 480


class ClientError(Exception):
    """Only static, non-sensitive messages may be displayed."""


def safe_error(error):
    """Never print exception text from HTTP/TLS libraries (URLs may contain tokens)."""
    if isinstance(error, ClientError):
        return str(error)
    if isinstance(error, aiohttp.ClientConnectorCertificateError):
        return "TLS certificate verification failed"
    if isinstance(error, aiohttp.ClientConnectorSSLError):
        return "TLS connection failed"
    if isinstance(error, aiohttp.ClientConnectorError):
        return "DNS resolution failed" if isinstance(error.os_error, socket.gaierror) else "Connection failed"
    if isinstance(error, aiohttp.WSServerHandshakeError):
        status = error.status if type(error.status) is int and 100 <= error.status <= 599 else "unknown"
        return f"WebSocket handshake failed: HTTP {status}"
    if isinstance(error, aiohttp.ContentTypeError):
        return "Expected a JSON response but received another content type"
    if isinstance(error, TimeoutError):
        return "Request timed out"
    if isinstance(error, aiohttp.ClientConnectionError):
        return "Connection closed or interrupted"
    if isinstance(error, json.JSONDecodeError):
        return "Invalid JSON response"
    if isinstance(error, aiohttp.ClientError):
        return "HTTP transport failed"
    return "Unexpected client error"


def validate_url(url, schemes, allow_insecure=False):
    parsed = urlsplit(url)
    if (parsed.scheme not in schemes or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ClientError("Invalid service URL")
    if (parsed.scheme in ("http", "ws") and not allow_insecure
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")):
        raise ClientError("Use TLS outside localhost, or explicitly pass --allow-insecure for homologation")
    return url.rstrip("/")


def playback_frame(epoch, seq, pcm):
    if len(pcm) != 960:
        raise ClientError("Invalid playback PCM size")
    return HEADER.pack(1, 1, 0, epoch, seq, SAMPLES) + pcm


def capture_pcm(raw):
    if len(raw) != 976:
        raise ClientError("Invalid capture frame size")
    version, kind, flags, epoch, seq, samples = HEADER.unpack_from(raw)
    if (version, kind, flags, epoch, samples) != (1, 2, 0, 0, SAMPLES):
        raise ClientError("Invalid capture frame header")
    return seq, raw[16:]


def tone_frame(seq):
    # Low-level 440 Hz tone, continuous phase, explicitly little endian.
    return struct.pack("<480h", *(int(4000 * math.sin(2 * math.pi * 440 * (seq * SAMPLES + i) / RATE))
                                 for i in range(SAMPLES)))


def check_wav(path):
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, RATE, "NONE"):
            raise ClientError("Playback WAV must be PCM signed 16-bit, mono, 24000 Hz")


class Client:
    def __init__(self, session, args, token):
        self.session, self.args, self.token = session, args, token
        self.rid = None

    async def rest(self, method, path, body=None, command=False):
        headers = {"Authorization": "Bearer " + self.token}
        if command:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        try:
            async with self.session.request(method, self.args.portal_url + "/v1/voice" + path,
                                            json=body, headers=headers, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    # Do not echo response bodies, URLs, headers or exception text.
                    raise ClientError(f"Public API returned HTTP {response.status}")
                data = await response.json()
                if not isinstance(data, dict):
                    raise ClientError("Invalid public API response")
                return data
        except ClientError:
            raise
        except Exception as error:
            raise ClientError("Portal API request failed: " + safe_error(error)) from None

    async def snapshot(self):
        return await self.rest("GET", f"/registrations/{self.rid}")

    async def command(self, cid, action):
        return await self.rest("POST", f"/registrations/{self.rid}/calls/{cid}/{action}", command=True)

    async def receive(self, ws, audio):
        frames = gaps = non_silent = 0
        previous = None
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.BINARY:
                    seq, pcm = capture_pcm(message.data)
                    if previous is not None and seq > previous + 1:
                        gaps += seq - previous - 1
                    previous = seq
                    audio.writeframesraw(pcm)
                    frames += 1
                    non_silent += any(abs(v) > 300 for v in struct.unpack("<480h", pcm))
                elif message.type == aiohttp.WSMsgType.TEXT:
                    event = json.loads(message.data)
                    if event.get("type") == "playback.cleared":
                        log("Playback cleared; already transmitted audio cannot be recalled.", flush=True)
                    elif event.get("type") == "audio.rejected":
                        raise ClientError("Playback frame rejected by worker")
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise ClientError("Media transport failed")
        finally:
            log(f"Capture: {frames} frames, {frames * .02:.2f}s PCM, "
                  f"{non_silent} frames above threshold, {gaps} sequence gaps. "
                  "This does not prove intelligible speech.", flush=True)

    async def send(self, ws, epoch):
        source = wave.open(str(self.args.wav), "rb") if self.args.wav else None
        started = time.monotonic()
        deadline = started
        seq = 0
        try:
            while True:
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                elapsed = time.monotonic() - started
                if self.args.clear_after is not None and elapsed >= self.args.clear_after:
                    await ws.send_json({"v": 1, "type": "playback.clear", "epoch": epoch})
                    log("Playback clear requested; no more audio will be sent.", flush=True)
                    break
                if source:
                    pcm = source.readframes(SAMPLES)
                    if not pcm:
                        break
                    pcm = pcm.ljust(960, b"\0")
                else:
                    if elapsed >= self.args.tone_seconds:
                        break
                    pcm = tone_frame(seq)
                await asyncio.wait_for(ws.send_bytes(playback_frame(epoch, seq, pcm)), 1)
                seq += 1
                # Never send a burst to catch up after an event-loop stall.
                deadline = max(deadline + .02, time.monotonic())
            log("Playback finished; speak into the phone to test capture.", flush=True)
            await asyncio.Future()  # Remain supervised until the call ends.
        finally:
            if source:
                source.close()

    async def call(self, cid):
        if not re.fullmatch(r"[a-f0-9]{32}", cid):
            raise ClientError("Invalid public call ID")
        current = (await self.snapshot()).get("call") or {}
        outgoing = current.get("direction") == "outgoing"
        if current.get("id") != cid or current.get("status") != ("ready" if outgoing else "ringing"):
            return  # Historical/replayed call; never answer it.
        tasks = []
        attached = False
        try:
            ticket = await self.rest("POST", f"/registrations/{self.rid}/calls/{cid}/media-authorizations")
            url = validate_url(ticket["url"], ("ws", "wss"), self.args.allow_insecure)
            async with self.session.ws_connect(url, headers={"Authorization": "Bearer " + ticket["token"]},
                                                protocols=["moonu.audio.v1"], heartbeat=2,
                                                max_msg_size=4096, compress=0) as ws:
                attached = True
                async with asyncio.timeout(self.args.max_call_seconds):
                    ready = await ws.receive_json(timeout=5)
                    if (ws.protocol != "moonu.audio.v1" or
                            any(ready.get(k) != v for k, v in {
                                "v": 1, "type": "media.ready", "format": "pcm_s16le",
                                "sampleRate": RATE, "channels": 1, "frameMs": 20}.items())
                            or type(ready.get("epoch")) is not int):
                        raise ClientError("Unsupported media format")
                    directory = Path(self.args.record_dir)
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path = directory / ("capture-" + uuid.uuid4().hex + ".wav")
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as output, wave.open(output, "wb") as audio:
                        audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                        receiver = asyncio.create_task(self.receive(ws, audio))
                        tasks.append(receiver)
                        try:
                            await self.command(cid, "dial" if outgoing else "answer")
                            log("Call requested; waiting for answer and connected media.", flush=True)
                            async with asyncio.timeout(35 if outgoing else 25):
                                while True:
                                    if receiver.done():
                                        receiver.result()
                                        raise ClientError("Media closed before answer")
                                    call = (await self.snapshot()).get("call") or {}
                                    if call.get("id") != cid or call.get("status") == "ended":
                                        return
                                    if call.get("status") == "answered":
                                        break
                                    await asyncio.sleep(.25)
                            log("Call answered. Playing test audio and recording inbound audio.", flush=True)
                            sender = asyncio.create_task(self.send(ws, ready["epoch"]))
                            tasks.append(sender)
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            log(f"Inbound recording: {path}", flush=True)
        except TimeoutError:
            log("Call/media time limit reached.", flush=True)
        finally:
            if attached or outgoing:
                try:
                    await self.command(cid, "hangup")
                except (ClientError, aiohttp.ClientError, TimeoutError):
                    log("Hangup not confirmed; worker media-disconnect timeout remains active.", flush=True)

    def log_event(self, event):
        kind = event.get("type")
        if kind in ("registration.state", "call.incoming", "call.created", "call.ready", "call.ringing", "call.answered", "call.ended"):
            log(f"Event: {kind}", flush=True)
        elif kind == "dtmf.received":
            digit = event.get("digit")
            if isinstance(digit, str) and re.fullmatch(r"[0-9A-D*#]", digit):
                log(f"Event: dtmf.received digit={digit}", flush=True)
            else:
                log("Event: dtmf.received (invalid digit)", flush=True)

    async def run(self):
        state = await self.rest("PUT", f"/extensions/{self.args.extension_id}/registration", {"connected": True})
        self.rid = state["id"]
        if not re.fullmatch(r"[a-f0-9]{32}", self.rid):
            raise ClientError("Invalid public registration ID")
        log(f"Registration {self.rid}: desired connected. Waiting for calls. "
              "Registration remains connected after exit (existing billing applies).", flush=True)
        if getattr(self.args, "log_dtmf", getattr(self, "show_dtmf_by_default", True)):
            log("Test client displays received DTMF digits, including retained event replay. "
                "Use test data only; do not enter PINs or payment information.", flush=True)
        else:
            log("DTMF digits are hidden and are not sent to the AI provider.", flush=True)
        cursor, active = "0-0", None
        seen = set()
        destination = getattr(self.args, "destination", None)
        outbound_id = None
        registration_started = time.monotonic()
        try:
            while True:
                if active and active.done():
                    try:
                        active.result()
                    except (ClientError, aiohttp.ClientError, TimeoutError) as error:
                        log(safe_error(error), flush=True)
                    active = None
                if destination and outbound_id is None:
                    state = await self.snapshot()
                    if state.get("effective") == "registered":
                        state = await self.rest("POST", f"/registrations/{self.rid}/calls",
                                                {"destination": destination}, command=True)
                        outbound_id = state["call"]["id"]
                        log("Outgoing call prepared; waiting for media readiness. No automatic redial.", flush=True)
                    elif time.monotonic() - registration_started > 30:
                        raise ClientError("Registration not ready for outgoing call")
                events = await self.rest("GET", f"/registrations/{self.rid}/events?after={cursor}")
                for event in events["items"]:
                    self.log_event(event)
                cursor = events["cursor"]
                if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", cursor):
                    raise ClientError("Invalid public event cursor")
                # Replay may contain multiple ended calls. Only answer the current
                # snapshot; never let historical events occupy the active slot.
                if not active:
                    current = (await self.snapshot()).get("call") or {}
                    cid = current.get("id")
                    if destination and outbound_id == cid and current.get("status") == "ended":
                        return
                    if (current.get("status") == ("ready" if destination else "ringing")
                            and (not destination or cid == outbound_id) and cid not in seen):
                        active = asyncio.create_task(self.call(cid))
                        seen.add(cid)
                if len(seen) > 2000:
                    seen.clear()
                await asyncio.sleep(.25)
        finally:
            if active:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            if outbound_id:
                try:
                    await self.command(outbound_id, "hangup")
                except (ClientError, aiohttp.ClientError, TimeoutError):
                    log("Outgoing cancellation not confirmed; worker timeouts remain active.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-url", default="http://localhost:8080")
    parser.add_argument("--extension-id", type=int, required=True, help="Portal extension database ID, not SIP number")
    parser.add_argument("--destination", help="Make one outgoing call to this number instead of waiting for incoming calls")
    parser.add_argument("--record-dir", default="recordings/voice-client")
    parser.add_argument("--wav", type=Path, help="Optional 24 kHz mono PCM16 WAV, otherwise a 440 Hz tone")
    parser.add_argument("--tone-seconds", type=float, default=3)
    parser.add_argument("--clear-after", type=float, help="Cancel playback after this many seconds")
    parser.add_argument("--max-call-seconds", type=float, default=60)
    parser.add_argument("--allow-insecure", action="store_true", help="Allow HTTP/WS outside localhost for homologation only")
    args = parser.parse_args()
    try:
        if args.destination is not None and not re.fullmatch(r"\+?[0-9]{1,20}", args.destination):
            raise ClientError("Invalid destination: use 1–20 digits with optional leading +")
        if (args.extension_id < 1 or not 1 <= args.max_call_seconds <= 600
                or not 0 < args.tone_seconds <= 600
                or (args.clear_after is not None and not 0 < args.clear_after <= 600)):
            raise ClientError("Invalid extension ID or time limit (maximum 600 seconds)")
        args.portal_url = validate_url(args.portal_url, ("http", "https"), args.allow_insecure)
        if args.wav:
            check_wav(args.wav)
        token = os.environ.get("MOONU_API_TOKEN") or getpass.getpass("Portal API Token (hidden): ")
        if not token or any(c.isspace() for c in token):
            raise ClientError("Invalid API Token input")

        async def run():
            # No default auth headers: API Token must never reach the media worker.
            async with aiohttp.ClientSession() as session:
                await Client(session, args, token).run()
        asyncio.run(run())
    except KeyboardInterrupt:
        log("Stopped. Registration remains connected; disconnect explicitly through Portal API.")
    except Exception as error:
        log(safe_error(error))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
