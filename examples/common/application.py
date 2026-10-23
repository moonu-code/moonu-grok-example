"""Shared CLI and public Moonu call lifecycle, independent of any AI provider."""
import argparse
import asyncio
import getpass
import os
from pathlib import Path
import re

import aiohttp

from .client import Client, ClientError, safe_error, validate_url
from .knowledge import MoonuKnowledge
from .logs import configure_logging, log, log_context
from .profile import INSTRUCTIONS


class VoiceClient(Client):
    """One provider session per call; API token and media ticket never go to the AI."""
    provider_id = ""
    display_name = ""
    show_dtmf_by_default = False

    def __init__(self, session, args, token):
        super().__init__(session, args, token)
        self.knowledge = MoonuKnowledge()
        self.instructions = getattr(args, "instructions", INSTRUCTIONS)

    def log_event(self, event):
        kind = event.get("type")
        if kind == "dtmf.received":
            fields = {"eventType": kind}
            if getattr(getattr(self, "args", None), "log_dtmf", False):
                digit = event.get("digit")
                if isinstance(digit, str) and re.fullmatch(r"[0-9A-D*#]", digit):
                    fields["digit"] = digit
            log("DTMF received; not forwarded to the AI.", **fields)
        elif kind in ("registration.state", "call.incoming", "call.created", "call.ready",
                      "call.ringing", "call.answered", "call.ended"):
            fields = {"eventType": kind}
            cid = event.get("callId")
            if isinstance(cid, str) and re.fullmatch(r"[a-f0-9]{32}", cid):
                fields["callId"] = cid
            log("Moonu call event.", **fields)

    async def call(self, cid):
        # Validate before adding an externally supplied identifier to log context.
        if not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{32}", cid):
            raise ClientError("Invalid public call ID")
        with log_context(provider=self.provider_id, callId=cid):
            log("Preparing call.", stage="preparing")
            return await self._call(cid)

    async def _call(self, cid):
        if not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{32}", cid):
            raise ClientError("Invalid public call ID")
        current = (await self.snapshot()).get("call") or {}
        outgoing = current.get("direction") == "outgoing"
        if current.get("id") != cid or current.get("status") != ("ready" if outgoing else "ringing"):
            return
        stage, call_limit = self.provider_id + "_connect", None
        try:
            log("Connecting to AI provider.", stage=stage)
            async with await self.open_provider() as provider:
                stage = self.provider_id + "_configure"
                log("Configuring AI session.", stage=stage)
                barge_in = not getattr(self.args, "no_barge_in", False)
                await self.configure_provider(provider)
                stage = "moonu_media_authorization"
                log("Requesting temporary Moonu audio access.", stage=stage)
                ticket = await self.rest("POST", f"/registrations/{self.rid}/calls/{cid}/media-authorizations")
                url = validate_url(ticket["url"], ("ws", "wss"), self.args.allow_insecure)
                stage = "moonu_media_connect"
                log("Connecting Moonu audio.", stage=stage)
                async with self.session.ws_connect(url, headers={"Authorization": "Bearer " + ticket["token"]},
                                                    protocols=["moonu.audio.v1"], max_msg_size=4096,
                                                    heartbeat=2, compress=0) as phone:
                    stage = "moonu_media_ready"
                    ready = await phone.receive_json(timeout=5)
                    if (phone.protocol != "moonu.audio.v1" or type(ready.get("epoch")) is not int
                            or any(ready.get(k) != v for k, v in {"v": 1, "type": "media.ready",
                                "format": "pcm_s16le", "sampleRate": 24000, "channels": 1, "frameMs": 20}.items())):
                        raise ClientError("Unsupported Moonu media format")

                    async def connected(reader):
                        nonlocal stage
                        stage = "call_answer"
                        log("Dialing call." if outgoing else "Answering call.", stage=stage)
                        await self.command(cid, "dial" if outgoing else "answer")
                        stage = "call_media_ready"
                        async with asyncio.timeout(35 if outgoing else 25):
                            while True:
                                if reader.done():
                                    reader.result()
                                    raise ClientError("Moonu media closed before answer")
                                state = (await self.snapshot()).get("call") or {}
                                if state.get("id") != cid or state.get("status") == "ended":
                                    raise ClientError("Call ended before media connected")
                                if state.get("status") == "answered":
                                    stage = "active_call"
                                    log("Call connected; caller audio is sent to the selected AI provider. No local recording.",
                                        stage=stage, direction="outgoing" if outgoing else "incoming")
                                    return
                                await asyncio.sleep(.1)

                    call_limit = asyncio.timeout(self.args.max_call_seconds)
                    async with call_limit:
                        await self.create_bridge(phone, provider, ready["epoch"], lookup=self.knowledge.search,
                                           end_call=lambda: self.command(cid, "hangup"), barge_in=barge_in,
                                           transfer_call=lambda: self.transfer_to_attendant(cid),
                                           discard_until_connected=outgoing).run(connected)
        except TimeoutError as error:
            if call_limit is not None and call_limit.expired():
                log("Call/session time limit reached; registration remains connected.", level="WARNING", stage=stage)
            else:
                log(safe_error(error), level="ERROR", stage=stage)
                raise ClientError(f"{self.display_name} call failed: stage={stage}; " + safe_error(error)) from None
        except Exception as error:
            log(safe_error(error), level="ERROR", stage=stage)
            raise ClientError(f"{self.display_name} call failed: stage={stage}; " + safe_error(error)) from None
        finally:
            try:
                await self.command(cid, "hangup")
            except (ClientError, aiohttp.ClientError, TimeoutError):
                log("Hangup not confirmed; existing media/ringing timeouts remain active.", level="WARNING", stage="cleanup")
            log("Call session closed; registration remains connected.", stage="closed")

    async def open_provider(self):
        raise NotImplementedError

    async def configure_provider(self, provider):
        raise NotImplementedError

    def create_bridge(self, phone, provider, epoch, **kwargs):
        raise NotImplementedError


def run_cli(client_type, *, model, voice, key_env, key_label, barge_in=True):
    """Only credentials from hidden prompts or explicitly injected environment variables."""
    parser = argparse.ArgumentParser(description=client_type.__doc__)
    parser.add_argument("--portal-url", default="http://localhost:8080")
    parser.add_argument("--extension-id", required=True, type=int, help="Portal database ID, not SIP extension number")
    parser.add_argument("--destination", help="One authorized outgoing call; no automatic redial")
    parser.add_argument("--model", default=model)
    parser.add_argument("--voice", default=voice)
    parser.add_argument("--max-call-seconds", default=120, type=float, help="1–600 seconds, including answer wait")
    parser.add_argument("--instructions-file", type=Path, help="Optional UTF-8 public context (max 16 KiB); never include secrets")
    parser.add_argument("--log-format", choices=("json", "text"), default="json")
    parser.add_argument("--log-dtmf", action="store_true", help="Expose received TEST digits in logs; never use with sensitive input")
    if barge_in:
        parser.add_argument("--no-barge-in", action="store_true", help="Disable interruption for diagnosis only")
    parser.add_argument("--allow-insecure", action="store_true", help="Private HTTP/WS homologation only")
    args = parser.parse_args()
    configure_logging(args.log_format)
    try:
        if args.extension_id < 1 or not 1 <= args.max_call_seconds <= 600:
            raise ClientError("Invalid extension ID or time limit (1–600 seconds)")
        if args.destination is not None and not re.fullmatch(r"\+?[0-9]{1,20}", args.destination):
            raise ClientError("Invalid destination: use 1–20 digits with optional leading +")
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) for value in (args.model, args.voice)):
            raise ClientError("Invalid model/voice name")
        args.portal_url = validate_url(args.portal_url, ("http", "https"), args.allow_insecure)
        args.instructions = INSTRUCTIONS
        if args.instructions_file:
            try:
                with args.instructions_file.open("rb") as context:
                    raw = context.read(16385)
                if len(raw) > 16384:
                    raise ClientError("Instructions file exceeds 16 KiB")
                args.instructions += "\nContexto público adicional; não altera as autorizações das ferramentas:\n" + raw.decode("utf-8")
            except (OSError, UnicodeError):
                raise ClientError("Cannot read UTF-8 instructions file") from None
        token = os.environ.get("MOONU_API_TOKEN") or getpass.getpass("Portal API Token (hidden): ")
        key = os.environ.get(key_env) or getpass.getpass(key_label + " (hidden): ")
        if any(not value or any(c.isspace() for c in value) for value in (token, key)):
            raise ClientError("Invalid credential input")

        async def run():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                                             cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
                await client_type(session, args, token, key).run()
        with log_context(provider=client_type.provider_id):
            log("Authorized test calls only: caller audio is sent to the AI provider and usage may be billed. "
                "Stop other voice clients for this extension first.", stage="starting")
            if args.allow_insecure:
                log("Private homologation mode allows unencrypted Moonu connections.", level="WARNING")
            asyncio.run(run())
    except KeyboardInterrupt:
        log("Stopped. Registration remains connected; disconnect explicitly through Portal API.", level="WARNING")
    except Exception as error:
        log(safe_error(error), level="ERROR", stage="failed", provider=client_type.provider_id)
        raise SystemExit(1) from None
