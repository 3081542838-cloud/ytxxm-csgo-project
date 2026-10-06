"""Decode observed Panorama state and engine pause output, never command echoes.

This is a read-only adapter. Deployment of session telemetry and native input
integration remain separately gated. In particular, the live seek exposes a
one-tick difference from the current parser model; do not silently normalize it.
"""
from datetime import datetime
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time

from cs2pov.adapters.console_log import ConsoleLog
from cs2pov.services.replay import ReplayEvidence
from cs2pov.storage.settings import DataError, local_path


PREFIX = re.compile(r"^(\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
REPORT = "[PanoramaScript] POV_READBACK "
PAUSED = re.compile(r"^CGameRules - paused on tick (\d+)$")
END_PAUSED = re.compile(r"^\[Demo\] Demo paused at engine time (\d+), demo tick (\d+)$")
STATE_FIELDS = {"sFileName", "nTick", "bIsPaused", "nObserverMode",
                "nSpectatingPlayerId", "bIsPlayingDemoFile", "bIsPlayingBroadcast"}


@dataclass(frozen=True)
class ReplaySnapshot:
    process_identity: object
    path: Path
    local_demo: bool
    tick: int
    paused: bool
    player_id: str
    first_person: bool
    observed_at: float


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class ReplayLogDecoder:
    def __init__(self, identity, nonce, *, clock=time.monotonic, wall=time.time):
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_]{16,64}", nonce):
            raise DataError("回读会话标识无效。")
        self.identity, self.nonce, self.clock, self.wall = identity, nonce, clock, wall
        self.sequence = 0
        self.proof = None
        self.pause_tick = None
        self.pending_rules_pause = None
        self.anchor = None
        self.last_source_time = None
        self.snapshot_value = None
        self.allow_tick_boundary = False

    def invalidate(self):
        self.proof = self.pause_tick = self.anchor = None
        self.pending_rules_pause = None
        self.snapshot_value = None

    def source_time(self, text):
        # Engine timestamps have no year or subseconds. Choose the nearest
        # calendar year, then carry the source age to the monotonic clock.
        now = self.wall()
        current = datetime.fromtimestamp(now)
        candidates = []
        for year in (current.year - 1, current.year, current.year + 1):
            try:
                candidates.append(datetime.strptime(f"{year}/{text}", "%Y/%m/%d %H:%M:%S").timestamp())
            except ValueError:
                continue
        if not candidates:
            raise ValueError("Invalid source timestamp")
        stamp = min(candidates, key=lambda candidate: abs(candidate - now))
        age = now - stamp
        if not 0 <= age <= 5:
            raise ValueError("Stale or future source timestamp")
        if self.last_source_time is not None and stamp < self.last_source_time:
            raise ValueError("Source timestamp went backwards")
        self.last_source_time = stamp
        return self.clock() - age

    def consume(self, line):
        match = PREFIX.fullmatch(line.rstrip("\r\n"))
        if not match:
            # Never treat arbitrary text, chat or echoed commands as state.
            return
        stamp, body = match.groups()
        relevant = (body.startswith(REPORT) or body.startswith("[PanoramaScript] POV_READBACK_ERROR")
                    or body.startswith("[Demo] Demo Skipping")
                    or body.startswith("CGameRules - ")
                    or body.startswith("[Demo] Demo paused at"))
        if not relevant:
            return
        try:
            observed_at = self.source_time(stamp)
            if body.startswith("[Demo] Demo Skipping") or body.startswith("CGameRules - unpaused"):
                self.invalidate()
                return
            paused = PAUSED.fullmatch(body)
            if paused:
                self.proof = self.anchor = None
                self.pause_tick = int(paused[1])
                self.pending_rules_pause = (self.pause_tick, stamp)
                return
            end = END_PAUSED.fullmatch(body)
            if end:
                paired = (self.pending_rules_pause == (int(end[1]), stamp)
                          and self.anchor is None and self.proof is None)
                self.pending_rules_pause = None
                if paired:
                    # The same server pause also emits an engine Demo-clock
                    # summary. Its Demo clock can differ from Panorama's
                    # displayed clock; keep the existing rules+Panorama join.
                    # The first valid report supplies the actual displayed
                    # tick. Later movement still fails the unchanged anchor.
                    return
                self.proof = None
                self.pause_tick = int(end[1])
                self.anchor = (int(end[2]), self.pause_tick, None)
                return
            if not body.startswith(REPORT):
                self.invalidate()
                return
            self.pending_rules_pause = None
            payload = body[len(REPORT):]
            if len(payload.encode("utf-8")) > 2048:
                raise ValueError("Telemetry exceeded bounded message size")
            report = json.loads(payload, object_pairs_hook=unique_object)
            if set(report) != {"nonce", "sequence", "context", "state", "xuid"}:
                raise ValueError("Unknown telemetry structure")
            if report["nonce"] != self.nonce or report["context"] != "HudDemoController":
                raise ValueError("Wrong telemetry session or context")
            sequence = report["sequence"]
            if type(sequence) is not int or sequence <= self.sequence:
                raise ValueError("Replayed sequence or reloaded context")
            state = report["state"]
            if not isinstance(state, dict) or set(state) != STATE_FIELDS:
                raise ValueError("Missing or unknown state fields")
            for field in ("nTick", "nObserverMode", "nSpectatingPlayerId"):
                if type(state[field]) is not int or state[field] < 0:
                    raise ValueError("Invalid integer state")
            for field in ("bIsPaused", "bIsPlayingDemoFile", "bIsPlayingBroadcast"):
                if type(state[field]) is not bool:
                    raise ValueError("Invalid boolean state")
            path = Path(local_path(state["sFileName"]))
            if path.suffix.casefold() != ".dem":
                raise ValueError("Not a local demo file")
            xuid = report["xuid"]
            if not isinstance(xuid, str) or not re.fullmatch(r"[1-9][0-9]{16}", xuid):
                raise ValueError("Missing stable player identity")
            # Main-menu contexts report state:null and restart their counter
            # when a Demo loads. Invalid state cannot advance the accepted
            # sequence and poison the fresh context; valid replay protection
            # still survives errors and never accepts an old valid sequence.
            self.sequence = sequence
            if not state["bIsPlayingDemoFile"] or state["bIsPlayingBroadcast"] or not state["bIsPaused"]:
                self.invalidate()
                self.snapshot_value = ReplaySnapshot(self.identity,path,
                    state["bIsPlayingDemoFile"] and not state["bIsPlayingBroadcast"],state["nTick"],
                    state["bIsPaused"],xuid,state["nObserverMode"]==2,observed_at)
                return
            self.snapshot_value = ReplaySnapshot(self.identity,path,True,state["nTick"],True,
                xuid,state["nObserverMode"]==2,observed_at)
            tick = state["nTick"]
            if self.anchor is not None:
                anchor_tick, server, anchor_path = self.anchor
                if ((tick != anchor_tick and not (self.allow_tick_boundary and abs(tick-anchor_tick) == 1))
                        or anchor_path is not None and path != anchor_path):
                    raise ValueError("State moved since pause evidence")
                # Preserve the engine's actual tick, rather than rewriting it
                # to the requested target or inventing a server clock.
                tick = anchor_tick
            elif self.pause_tick is not None:
                server = self.pause_tick
            else:
                self.proof = None
                return
            self.anchor = (tick, server, path)
            # Qualified 1.41.8.8 / 1.41.8.9: observed first person=2.
            # Player identity alone is insufficient to classify the camera.
            self.proof = ReplayEvidence(self.identity, True, path, tick, server,
                                        True, xuid, state["nObserverMode"] == 2, observed_at)
        except (ValueError, TypeError, KeyError, RecursionError):
            self.invalidate()

    def readback(self):
        if self.proof is None or not 0 <= self.clock() - self.proof.observed_at <= 5:
            return None
        return self.proof

    def snapshot(self):
        value = self.snapshot_value
        return value if value is not None and 0 <= self.clock()-value.observed_at <= 5 else None


class ReplayLogReader:
    """Starts after a fresh checkpoint; failures invalidate previously held proof."""
    def __init__(self, session, identity, nonce, *, clock=time.monotonic, wall=time.time):
        self.log = ConsoleLog(session)
        self.cursor = self.log.checkpoint()
        self.decoder = ReplayLogDecoder(identity, nonce, clock=clock, wall=wall)

    def readback(self):
        try:
            content, cursor = self.log.read_after(self.cursor)
            for line in content.splitlines():
                self.decoder.consume(line)
            self.cursor = cursor
            return self.decoder.readback()
        except (OSError, DataError):
            self.decoder.invalidate()
            raise

    def snapshot(self):
        self.readback()
        return self.decoder.snapshot()
