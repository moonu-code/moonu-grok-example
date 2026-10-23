#!/usr/bin/env python3
"""Moonu calls with Grok; public APIs only, no local recording."""
import asyncio
import base64
import json
import re
import uuid

import aiohttp

# Support both "python -m examples.PROVIDER" and direct execution from any directory.
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.common.application import VoiceClient, run_cli
from examples.common.client import ClientError
from examples.common.audio import PlaybackOverflow, PlaybackPacer, send_json
from examples.common.realtime import RealtimeBridge as Bridge
from examples.common.profile import INSTRUCTIONS, END_TOOL, END_REASONS, TRANSFER_TOOL
from examples.common.knowledge import TOOL
from examples.common.dtmf import send_digits
from examples.common.logs import log

GROK_URL = "wss://api.x.ai/v1/realtime"
DEFAULT_MODEL = "grok-voice-3.0"


def session_config(voice, instructions=INSTRUCTIONS):
    pcm = {"type": "audio/pcm", "rate": 24000}
    # xAI's voice and turn_detection are top-level session fields, not OpenAI GA's schema.
    return {"type": "session.update", "session": {
        "voice": voice, "instructions": instructions,
        "turn_detection": {"type": "server_vad"},
        "audio": {"input": {"format": pcm, "transcription": {"language_hint": "pt-BR"}},
                  "output": {"format": pcm}},
        "tools": [TOOL, END_TOOL, TRANSFER_TOOL]}}


def message_json(message):
    if message.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
        raise ClientError("Grok connection closed or failed; check API access and quota")
    try:
        event = json.loads(message.data)
    except (ValueError, TypeError, UnicodeError):
        raise ClientError("Grok returned invalid JSON") from None
    if not isinstance(event, dict):
        raise ClientError("Grok returned an invalid event")
    return event


async def configure(grok, voice, instructions=INSTRUCTIONS):
    await send_json(grok, session_config(voice, instructions))
    async with asyncio.timeout(10):
        while True:
            event = message_json(await grok.receive())
            if event.get("type") == "error":
                raise ClientError("Grok rejected configuration; check model, voice, key and quota")
            if event.get("type") == "session.updated":
                audio = event.get("session", {}).get("audio", {})
                for direction in ("input", "output"):
                    if audio.get(direction, {}).get("format") != {"type": "audio/pcm", "rate": 24000}:
                        raise ClientError("Grok did not confirm PCM16/24kHz audio")
                return


class GrokBridge(Bridge):
    """Grok protocol adapter; shared audio and call actions live in common/."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_tools = {}
        self.response_turns = {}

    async def limit_generation(self, response_id):
        self.generation_limited.add(response_id)
        self.pending.clear()
        self.pending_key = None
        if self.active_response == response_id:
            event_id = "cancel_" + uuid.uuid4().hex
            self.cancel_requests.append(event_id)
            await send_json(self.ai, {"type": "response.cancel", "event_id": event_id})
        log("Grok generation limited: buffered playback continues; call remains connected.", flush=True)

    def collect_tool(self, rid, call):
        cid = call.get("call_id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid):
            raise ClientError("Invalid Grok tool identifier")
        if cid in self.seen_tools:
            return
        if not isinstance(call.get("arguments"), str) or len(call["arguments"]) > 2048:
            call = {"call_id": cid, "name": "unsupported", "arguments": "null"}
        else:
            call = {key: call.get(key) for key in ("call_id", "name", "arguments")}
        batch = self.pending_tools.setdefault(rid, {})
        if cid in batch and batch[cid] != call:
            raise ClientError("Conflicting Grok tool call")
        batch[cid] = call
        if sum(len(calls) for calls in self.pending_tools.values()) > 4:
            raise ClientError("Grok tool concurrency limit exceeded")

    async def ai_event(self, event):
        kind = event.get("type")
        if kind == "error":
            error = event.get("error", {})
            eid, code = error.get("event_id"), error.get("code")
            if eid in self.cancel_requests and code == "response_cancel_not_active":
                self.cancel_requests.remove(eid)
                return
            if eid in self.tool_resumes and code == "conversation_already_has_active_response":
                self.tool_resumes.remove(eid)
                return
            raise ClientError("Grok returned an error; check API key, quota and voice model configuration")
        if self.handoff:
            if kind == "response.created":
                await send_json(self.ai, {"type": "response.cancel"})
            return
        if kind == "response.created":
            rid = event.get("response", {}).get("id")
            if not isinstance(rid, str) or not 1 <= len(rid) <= 128:
                raise ClientError("Invalid Grok response identifier")
            if len(self.response_turns) >= 128:
                raise ClientError("Grok response limit exceeded")
            self.response_turns[rid] = self.turn
        elif kind == "response.function_call_arguments.done":
            rid = event.get("response_id", self.current_response)
            if rid not in self.response_turns:
                raise ClientError("Grok tool without a known response")
            if rid not in self.cancelled and self.response_turns[rid] == self.turn:
                self.collect_tool(rid, event)
            return  # Batch until response.done: parallel tools resume only once.
        elif kind == "response.done":
            response = event.get("response", {})
            rid = response.get("id")
            if rid not in self.response_turns:
                raise ClientError("Grok completed an unknown response")
            if response.get("status") == "failed":
                raise ClientError("Grok response failed; check model access and quota")
            if self.active_response == rid:
                self.active_response = None
            actionable = (response.get("status") == "completed" and rid not in self.cancelled
                          and self.response_turns[rid] == self.turn and not self.speaking)
            if actionable:
                for item in response.get("output", []):
                    if item.get("type") == "function_call":
                        self.collect_tool(rid, item)
            calls = list(self.pending_tools.pop(rid, {}).values())
            if actionable and self.pending_key and self.pending_key[0] == rid:
                try:
                    self.enqueue(self.pending_key, b"", final=True)
                except PlaybackOverflow:
                    await self.limit_generation(rid)
            if actionable and calls:
                if len(self.seen_tools) + len(calls) > 128 or self.tool_jobs.full():
                    raise ClientError("Grok tool call limit exceeded")
                self.seen_tools.update(call["call_id"] for call in calls)
                self.tool_jobs.put_nowait((self.turn, calls))
            usage = response.get("usage") or {}
            counts = {key: value for key, value in usage.items()
                      if key in ("input_tokens", "output_tokens", "total_tokens")
                      and type(value) is int and 0 <= value <= 1000000000}
            log("Grok response ended; usage=" + json.dumps(counts, sort_keys=True), flush=True)
            return
        # The documented xAI audio/VAD/truncate events share the realtime wire format.
        await super().ai_event(event)

    async def ai_reader(self):
        async for message in self.ai:
            await self.ai_event(message_json(message))
        raise ClientError("Grok session closed; no automatic reconnect or redial")

    async def tool_worker(self):
        while True:
            turn, calls = await self.tool_jobs.get()
            for call in sorted(calls, key=lambda c: c.get("name") != "transferir_atendimento"):
                if self.handoff:
                    break
                result = await self.execute_tool(call, turn)
                if result.get("status") == "ended":
                    return
                if not self.handoff and (turn != self.turn or self.speaking):
                    result = {"status": "cancelled_new_turn"}
                await send_json(self.ai, {"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.dumps(result, ensure_ascii=False)}})
            if not self.handoff and turn == self.turn and not self.speaking and self.active_response is None:
                eid = "lookup_" + uuid.uuid4().hex
                self.tool_resumes.append(eid)
                await send_json(self.ai, {"type": "response.create", "event_id": eid})

    async def start_provider(self):
        await send_json(self.ai, {"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text":
                "A chamada foi atendida. Apresente-se brevemente conforme as instruções e aguarde."}]}})
        await super().start_provider()


class GrokClient(VoiceClient):
    """Grok voice agent connected through the public Moonu API."""
    provider_id = "grok"
    display_name = "Grok"

    def __init__(self, session, args, token, xai_key):
        super().__init__(session, args, token)
        self.xai_key = xai_key

    async def open_grok(self):
        try:
            return await asyncio.wait_for(self.session.ws_connect(
                GROK_URL, params={"model": self.args.model}, headers={"Authorization": "Bearer " + self.xai_key},
                max_msg_size=2 * 1024 * 1024, heartbeat=10, compress=0), 10)
        except aiohttp.WSServerHandshakeError as error:
            raise ClientError(f"Grok WebSocket returned HTTP {error.status}; check API key, model and quota") from None

    async def transfer_to_attendant(self, cid):
        await send_digits(self, cid, "#")
        await asyncio.sleep(.8)
        await send_digits(self, cid, "500")

    async def open_provider(self):
        return await self.open_grok()

    async def configure_provider(self, provider):
        await configure(provider, self.args.voice, instructions=self.instructions)

    def create_bridge(self, phone, provider, epoch, **kwargs):
        return GrokBridge(phone, provider, epoch, **kwargs)


def main():
    run_cli(GrokClient, model=DEFAULT_MODEL, voice="eve",
            key_env="XAI_API_KEY", key_label="Grok API key", barge_in=False)


if __name__ == "__main__":
    main()
