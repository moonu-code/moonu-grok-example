"""Realtime wire-protocol helpers shared by OpenAI Realtime and xAI Grok."""
import asyncio
import base64
import json
import re
import uuid

import aiohttp

from .audio import AudioBridge, PlaybackOverflow, send_json
from .client import ClientError
from .logs import log

class RealtimeBridge(AudioBridge):
    async def tool_worker(self):
        while True:
            turn, calls = await self.tool_jobs.get()
            # A transfer request must never be turned into hangup by a second
            # tool in the same model response, regardless of output order.
            calls = sorted(calls, key=lambda c: c.get("name") != "transferir_atendimento")
            exhausted = False
            for call in calls:
                if self.handoff:
                    break  # Do not run another tool or resume AI speech after handoff.
                if call.get("name") == "transferir_atendimento":
                    status = await self.transfer(call, turn)
                    await send_json(self.ai, {"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": call["call_id"],
                        "output": json.dumps({"status": status, "transferConfirmed": False})}})
                    exhausted = True
                    if self.handoff:
                        break
                    continue
                if call.get("name") == "encerrar_chamada":
                    status = ("transfer_not_hangup" if self.transfer_attempted
                              else await self.finish_call(call, turn))
                    if status == "ended":
                        return  # Supervised completion closes both media sessions.
                    await send_json(self.ai, {"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": call["call_id"],
                        "output": json.dumps({"status": status})}})
                    exhausted = True  # Do not retry an invalid/undrained hangup in a tool loop.
                    continue
                self.tool_count += 1
                result = {"status": "lookup_unavailable", "results": []}
                if self.tool_count > 20:
                    result, exhausted = {"status": "lookup_limit", "results": []}, True
                elif self.lookup and call.get("name") == "consultar_moonu":
                    try:
                        arguments = json.loads(call.get("arguments", ""))
                        if not isinstance(arguments, dict) or set(arguments) != {"query"}:
                            raise ValueError("Invalid tool arguments")
                        log("Moonu public lookup started.", flush=True)
                        result = await asyncio.wait_for(self.lookup(arguments["query"]), 12)
                        log("Moonu public lookup completed.", flush=True)
                    except (ValueError, TypeError, aiohttp.ClientError, OSError, TimeoutError):
                        result = {"status": "lookup_unavailable", "results": []}
                await send_json(self.ai, {"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call["call_id"],
                    "output": json.dumps(result, ensure_ascii=False)}})
            # Lookup runs off the media reader. A newer caller turn must not be
            # interrupted by a late answer to an earlier question.
            if not self.handoff and turn == self.turn and not self.speaking and self.active_response is None:
                event_id = "lookup_" + uuid.uuid4().hex
                self.tool_resumes.append(event_id)
                event = {"type": "response.create", "event_id": event_id}
                if exhausted:
                    event["response"] = {"tool_choice": "none"}
                await send_json(self.ai, event)

    async def limit_generation(self, response_id):
        # Generation and playback are separate: reaching the local buffer limit
        # must NOT erase the speech that is already queued for the caller.
        self.generation_limited.add(response_id)
        self.pending.clear()
        self.pending_key = None
        if self.active_response == response_id:
            event_id = "cancel_" + uuid.uuid4().hex
            self.cancel_requests.append(event_id)
            await send_json(self.ai, {"type": "response.cancel", "response_id": response_id,
                                     "event_id": event_id})
        log(f"AI generation limited: buffered playback continues; queuedMs={len(self.output) * 20}, "
              f"receivedMs={self.received_bytes // 48}, sentMs={self.sent_frames * 20}. "
              "Call remains connected.", flush=True)

    async def interrupt(self):
        # Server VAD with interrupt_response=true cancels generation at OpenAI.
        # Cancel locally even if response.done preceded speech_started: playback may lag.
        async with self.media_lock:
            if self.current_response:
                self.cancelled.add(self.current_response)
            self.cancelled.update(key[0] for key, _, _ in self.output)
            if self.pending_key:
                self.cancelled.add(self.pending_key[0])
            self.output.clear()
            self.pending.clear()
            self.pending_key = None
            self.available.clear()
            self.cleared.clear()
            self.clearing = True
            await send_json(self.phone, {"v": 1, "type": "playback.clear", "epoch": self.epoch})
        await asyncio.wait_for(self.cleared.wait(), 3)
        truncate_item = self.last_item
        if truncate_item:
            # A transport estimate only, NOT proof of audio heard by the caller.
            frames = (max(0, min(self.item_sent, self.pulled - self.item_start))
                      if truncate_item == self.last_item else 0)
            duration_ms = min(frames * 480, self.item_samples) * 1000 // 24000
            await send_json(self.ai, {"type": "conversation.item.truncate",
                                     "item_id": truncate_item, "content_index": 0,
                                     "audio_end_ms": duration_ms})
        self.sent_total = self.pulled  # Clear discarded any queued, not-yet-pulled frames.
        self.last_item = None
        self.item_sent = self.item_samples = 0
        log("Caller interruption: playback cleared; cancelled-response audio discarded.", flush=True)

    async def input_writer(self):
        while True:
            pcm = await self.input.get()
            if self.handoff:
                continue  # Don't let the Asterisk feature prompt become another AI turn.
            await send_json(self.ai, {"type": "input_audio_buffer.append",
                                     "audio": base64.b64encode(pcm).decode("ascii")})

    async def ai_event(self, event):
        kind = event.get("type")
        if self.handoff and kind == "response.created":
            event_id = "cancel_" + uuid.uuid4().hex
            self.cancel_requests.append(event_id)
            await send_json(self.ai, {"type": "response.cancel", "response_id": event["response"]["id"],
                                     "event_id": event_id})
            return
        if self.handoff and kind in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                                      "response.output_audio.delta", "response.output_audio.done"):
            return
        if kind == "error":
            error = event.get("error", {})
            event_id = error.get("event_id")
            if (event_id in self.tool_resumes
                    and error.get("code") == "conversation_already_has_active_response"):
                self.tool_resumes.remove(event_id)
                return
            if (event_id in self.cancel_requests
                    and error.get("code") == "response_cancel_not_active"):
                # Generation can finish before our targeted cancel is processed.
                # Only this known race for one of our own commands is benign.
                self.cancel_requests.remove(event_id)
                return
            # Provider messages may contain input, identifiers or credentials.
            raise ClientError("AI provider returned an error; check account, quota and model configuration")
        if kind == "response.created":
            self.current_response = event["response"]["id"]
            self.active_response = self.current_response
        elif kind == "input_audio_buffer.speech_started":
            self.turn += 1
            self.speaking = True
            if self.barge_in:
                await self.interrupt()
            else:
                log("Speech detected; barge-in disabled, playback not cleared.", flush=True)
        elif kind == "input_audio_buffer.speech_stopped":
            self.speaking = False
        elif kind in ("response.output_audio.delta", "response.output_audio.done"):
            rid = event["response_id"]
            if rid in self.cancelled or rid in self.generation_limited:
                return
            key = (rid, event["item_id"], event.get("content_index", 0))
            if key[2] != 0:
                raise ClientError("Unsupported provider audio content index")
            try:
                if kind.endswith(".delta"):
                    encoded = event["delta"]
                    if len(encoded) > 1024 * 1024:
                        raise ClientError("Provider audio chunk exceeds demo limit")
                    decoded = base64.b64decode(encoded, validate=True)
                    self.received_bytes += len(decoded)
                    self.enqueue(key, decoded)
                else:
                    self.enqueue(key, b"", final=True)
            except PlaybackOverflow:
                await self.limit_generation(rid)
        elif kind == "response.done":
            if event.get("response", {}).get("id") == self.active_response:
                self.active_response = None
            if event.get("response", {}).get("status") == "failed":
                raise ClientError("AI provider response failed")
            response = event.get("response", {})
            status = response.get("status")
            if status not in ("completed", "cancelled", "incomplete", "failed"):
                status = "unknown"
            reason = (response.get("status_details") or {}).get("reason")
            if reason not in (None, "turn_detected", "client_cancelled", "max_output_tokens", "content_filter"):
                reason = "unknown"
            output_tokens = (response.get("usage") or {}).get("output_tokens")
            if type(output_tokens) is not int or not 0 <= output_tokens <= 10000000:
                output_tokens = None
            log(f"AI response ended: status={status}, reason={reason}, outputTokens={output_tokens}, "
                  f"clientBufferLimited={response.get('id') in self.generation_limited}.", flush=True)
            if response.get("status") == "completed":
                calls = []
                for item in response.get("output", []):
                    if item.get("type") != "function_call":
                        continue
                    cid = item.get("call_id", "")
                    if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid):
                        raise ClientError("Invalid lookup call identifier")
                    if cid in self.seen_tools:
                        continue
                    self.seen_tools.add(cid)
                    if not isinstance(item.get("arguments"), str) or len(item["arguments"]) > 2048:
                        item = {"call_id": cid}  # Return a safe unavailable result, never execute it.
                    calls.append(item)
                if calls:
                    if len(calls) > 4 or self.tool_jobs.full():
                        raise ClientError("Lookup concurrency limit exceeded")
                    self.tool_jobs.put_nowait((self.turn, calls))

    async def ai_reader(self):
        async for message in self.ai:
            if message.type == aiohttp.WSMsgType.TEXT:
                await self.ai_event(json.loads(message.data))
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ClientError("AI provider transport failed")
        raise ClientError("AI provider session closed")

    async def start_provider(self):
        await send_json(self.ai, {"type": "response.create"})
