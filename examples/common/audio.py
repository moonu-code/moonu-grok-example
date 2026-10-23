"""Provider-neutral audio framing, bounded playback, pacing and guarded call actions."""
import asyncio
from collections import deque
import json
import time

import aiohttp

from .client import ClientError, capture_pcm, playback_frame
from .profile import END_REASONS
from .logs import log

async def send_json(ws, event):
    """Bound every write; never log a raw provider event."""
    await asyncio.wait_for(ws.send_json(event), 2)

class PlaybackOverflow(Exception):
    """Internal flow-control signal, not a call failure."""


class PlaybackPacer:
    """Absolute 20 ms deadlines; small worker preroll, never unbounded catch-up."""
    def __init__(self):
        self.epoch = self.next_at = None
        self.preroll = 0
        self.rebases = 0

    def target(self, now, epoch):
        if epoch != self.epoch:
            self.epoch, self.next_at, self.preroll = epoch, now, 5
        elif now - self.next_at > .1:
            self.next_at, self.preroll = now, 5
            self.rebases += 1
        return self.next_at

    def sent(self, now):
        self.preroll = max(0, self.preroll - 1)
        self.next_at = now if self.preroll else self.next_at + .02


class AudioBridge:
    """Shared media transport; subclasses implement the provider protocol."""
    def __init__(self, phone, ai, epoch, output_capacity=3000, lookup=None, end_call=None, barge_in=True,
                 transfer_call=None, discard_until_connected=False):
        self.phone, self.ai, self.epoch = phone, ai, epoch
        self.input = asyncio.Queue(maxsize=50)  # 1 second; fail rather than lose speech silently.
        self.capture_ready = not discard_until_connected
        self.output, self.output_capacity = deque(), output_capacity  # 60 seconds / 2.88 MB raw PCM maximum.
        self.available, self.cleared = asyncio.Event(), asyncio.Event()
        self.media_lock = asyncio.Lock()
        self.clearing = False
        self.seq = self.sent_total = self.pulled = 0
        self.current_response = None
        self.active_response = None
        self.cancel_requests = deque(maxlen=32)
        self.cancelled = set()
        self.generation_limited = set()
        self.pending = bytearray()
        self.pending_key = None
        self.last_item = None
        self.item_start = self.item_sent = self.item_samples = 0
        self.rejections = {}
        self.received_bytes = self.sent_frames = self.queue_peak = 0
        self.pacer = PlaybackPacer()
        self.lookup = lookup
        self.tool_jobs = asyncio.Queue(maxsize=4)
        self.seen_tools = set()
        self.tool_count = self.turn = 0
        self.speaking = False
        self.tool_resumes = deque(maxlen=32)
        self.end_call, self.barge_in = end_call, barge_in
        self.transfer_call = transfer_call
        self.transfer_attempted = self.handoff = False

    async def transfer(self, call, turn):
        try:
            arguments = json.loads(call.get("arguments", ""))
        except (ValueError, TypeError):
            arguments = None
        if arguments != {} or not self.transfer_call:
            return "invalid_transfer_request"
        if self.transfer_attempted:
            return "transfer_already_attempted"
        if turn != self.turn or self.speaking:
            return "cancelled_new_turn"
        try:
            async with asyncio.timeout(10):
                while self.output or self.pending:
                    if turn != self.turn or self.speaking:
                        return "cancelled_new_turn"
                    await asyncio.sleep(.02)
                await asyncio.sleep(1)  # Bounded allowance, not proof the announcement was heard.
        except TimeoutError:
            return "playback_not_drained"
        if turn != self.turn or self.speaking:
            return "cancelled_new_turn"
        # From the first digit onward, never replay a partial/uncertain feature code.
        self.transfer_attempted = self.handoff = True
        try:
            await self.transfer_call()
        except (ClientError, aiohttp.ClientError, OSError, TimeoutError):
            self.handoff = False
            log("Transfer submission unconfirmed; no automatic retry or hangup.", flush=True)
            return "transfer_unconfirmed"
        log("Transfer digits submitted; waiting for telephony. Transfer completion is not confirmed.", flush=True)
        return "transfer_submitted"

    async def finish_call(self, call, turn):
        try:
            args = json.loads(call.get("arguments", ""))
        except (ValueError, TypeError):
            args = None
        if (not isinstance(args, dict) or set(args) != {"reason"}
                or args["reason"] not in END_REASONS or not self.end_call):
            return "invalid_end_request"
        if turn != self.turn or self.speaking:
            return "cancelled_new_turn"
        # response.done means generation finished, not that the phone heard it.
        # Allow a short goodbye to drain, then a bounded worker/network allowance.
        try:
            async with asyncio.timeout(10):
                while self.output or self.pending:
                    if turn != self.turn or self.speaking:
                        return "cancelled_new_turn"
                    await asyncio.sleep(.02)
                await asyncio.sleep(1)
        except TimeoutError:
            return "playback_not_drained"
        if turn != self.turn or self.speaking:
            return "cancelled_new_turn"
        await self.end_call()
        log("AI requested public hangup after confirmed conversation end; registration unchanged.", flush=True)
        return "ended"

    def enqueue(self, key, pcm, final=False):
        if key[0] in self.cancelled or key[0] in self.generation_limited:
            return
        if self.pending_key not in (None, key) and self.pending:
            raise ClientError("Unexpected overlapping provider audio items")
        self.pending_key = key
        self.pending.extend(pcm)
        while len(self.pending) >= 960 or (final and self.pending):
            if len(self.output) >= self.output_capacity:
                raise PlaybackOverflow()
            chunk = bytes(self.pending[:960])
            del self.pending[:960]
            if len(chunk) % 2:
                raise ClientError("Invalid provider PCM alignment")
            self.output.append((key, chunk.ljust(960, b"\0"), len(chunk) // 2))
            self.queue_peak = max(self.queue_peak, len(self.output))
            self.available.set()

    async def phone_reader(self):
        async for message in self.phone:
            if message.type == aiohttp.WSMsgType.BINARY:
                _, pcm = capture_pcm(message.data)
                if not self.capture_ready:
                    continue  # Early ringback/announcements are not a conversation with the callee.
                try:
                    self.input.put_nowait(pcm)
                except asyncio.QueueFull:
                    raise ClientError("AI input stalled; capture queue full") from None
            elif message.type == aiohttp.WSMsgType.TEXT:
                event = json.loads(message.data)
                if event.get("type") == "playback.cleared":
                    epoch = event.get("epoch")
                    if not self.clearing or type(epoch) is not int or epoch <= self.epoch:
                        raise ClientError("Unexpected playback clear acknowledgement")
                    self.epoch = event["epoch"]
                    self.seq = 0
                    self.pulled = event.get("trackPulledFrames", 0)
                    self.clearing = False
                    self.cleared.set()
                elif event.get("type") == "audio.rejected":
                    reason = event.get("reason")
                    if reason not in ("stale_epoch", "queue_full", "duplicate_sequence"):
                        raise ClientError("Worker rejected AI playback frame with unknown reason")
                    epoch = event.get("epoch")
                    if type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
                        raise ClientError("Invalid worker playback epoch")
                    self.rejections[reason] = self.rejections.get(reason, 0) + 1
                    async with self.media_lock:
                        # Recovery for an older worker changing epoch during a
                        # scheduler pause. In-flight old rejections must not reset
                        # the sequence again after we have adopted the new epoch.
                        if epoch > self.epoch and not self.clearing:
                            self.epoch, self.seq = epoch, 0
                    if self.rejections[reason] == 1:
                        log(f"Playback recovery: {reason}; call remains connected.", flush=True)
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ClientError("Moonu media transport failed")

    async def output_writer(self):
        while True:
            await self.available.wait()
            now = time.monotonic()
            await asyncio.sleep(max(0, self.pacer.target(now, self.epoch) - now))
            async with self.media_lock:
                if self.clearing or not self.output:
                    self.available.clear()
                    continue
                key, pcm, samples = self.output.popleft()
                if not self.output:
                    self.available.clear()
                if key[0] in self.cancelled:
                    continue
                if self.last_item != key[1]:
                    self.last_item = key[1]
                    self.item_start = self.sent_total
                    self.item_sent = self.item_samples = 0
                await asyncio.wait_for(self.phone.send_bytes(playback_frame(self.epoch, self.seq, pcm)), 1)
                self.seq += 1
                self.sent_total += 1
                self.sent_frames += 1
                self.item_sent += 1
                self.item_samples += samples
                self.pacer.sent(time.monotonic())

    async def run(self, connected):
        tasks = [asyncio.create_task(self.phone_reader())]
        try:
            # Receive capture while waiting for Moonu answer, but don't send AI audio early.
            await connected(tasks[0])
            self.capture_ready = True
            tasks.extend(asyncio.create_task(coro) for coro in
                         (self.input_writer(), self.ai_reader(), self.output_writer(), self.tool_worker()))
            # Inherit session instructions, including language/accent, from the
            # first response; per-response instructions would override that policy.
            await self.start_provider()
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.rejections:
                log("Playback rejection totals: " + json.dumps(self.rejections, sort_keys=True), flush=True)
            log(f"AI media totals: receivedMs={self.received_bytes // 48}, "
                  f"sentMs={self.sent_frames * 20}, peakQueuedMs={self.queue_peak * 20}, "
                  f"pacingRebases={self.pacer.rebases}. "
                  "Sent audio is not confirmed heard audio.", flush=True)

    async def start_provider(self):
        raise NotImplementedError("Provider must implement its greeting")

    async def input_writer(self):
        raise NotImplementedError("Provider must implement input streaming")

    async def ai_reader(self):
        raise NotImplementedError("Provider must implement event handling")

    async def tool_worker(self):
        raise NotImplementedError("Provider must implement tool responses")

    async def execute_tool(self, call, turn):
        if turn != self.turn or self.speaking:
            return {"status": "cancelled_new_turn"}
        if call["name"] == "transferir_atendimento":
            return {"status": await self.transfer(call, turn), "transferConfirmed": False}
        if call["name"] == "encerrar_chamada":
            status = "transfer_not_hangup" if self.transfer_attempted else await self.finish_call(call, turn)
            return {"status": status}
        if call["name"] != "consultar_moonu":
            return {"status": "unsupported_tool"}
        self.tool_count += 1
        if self.tool_count > 20:
            return {"status": "lookup_limit", "results": []}
        try:
            args = json.loads(call.get("arguments", ""))
        except (ValueError, TypeError):
            return {"status": "invalid_lookup_request", "results": []}
        if not self.lookup or not isinstance(args, dict) or set(args) != {"query"}:
            return {"status": "invalid_lookup_request", "results": []}
        try:
            log("Moonu public lookup started.", flush=True)
            result = await asyncio.wait_for(self.lookup(args["query"]), 12)
            log("Moonu public lookup completed.", flush=True)
            return result
        except (ValueError, TypeError, aiohttp.ClientError, OSError, TimeoutError):
            return {"status": "lookup_unavailable", "results": []}
