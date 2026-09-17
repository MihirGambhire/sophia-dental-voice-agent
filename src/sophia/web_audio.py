"""
Call audio over a WebSocket, in the simplest wire format that works.

WHY THIS EXISTS
===============
The first voice transport was WebRTC, which sends audio directly between
the caller's browser and the server. That worked on the machine running
the server and failed for everyone else, and three rounds of fixes did not
change that:

  1. The server sits on an office network that blocks outbound UDP to
     ports 80 and 443, so direct media and UDP relaying both fail.
  2. A TURN relay fixed the browser side but not the server side. The
     server's WebRTC library uses only the first relay address it is
     given, and when it did allocate one, binding a channel to a caller on
     Jio mobile IPv6 was refused with 401 Unauthorized.
  3. Every fix was only testable from the same machine as the server,
     which is exactly the case that already worked.

The web page itself reached every caller without trouble, over HTTPS
through the same tunnel. So the audio now travels that way too: one
WebSocket, carried over the connection that is already proven to work.
There is no direct path to negotiate, nothing for a firewall to block, and
no relay to authenticate against.

The cost is a little latency, because audio goes through the tunnel rather
than peer to peer, and TCP retransmits lost packets instead of skipping
them. For a receptionist call that is a good trade for connecting at all.

THE WIRE FORMAT
===============
Deliberately trivial, so the browser side needs no library:

  browser -> server   binary message: raw 16 kHz mono signed 16 bit PCM
  server  -> browser  binary message: the same format, Sophia's voice
  server  -> browser  text message:   {"type": "clear"} to stop playback
  browser -> server   text message:   {"type": "text", "text": "..."}, the caller
                                      typing instead of speaking
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pipecat.frames.frames import (
    DataFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

SAMPLE_RATE = 16000
CHANNELS = 1

# Longer than anyone says in one breath, short enough to stop abuse.
MAX_TYPED_LENGTH = 500


@dataclass
class TypedTextFrame(DataFrame):
    """
    Something the caller typed during a call instead of saying.

    Added for a tester in a public place: they type, and Sophia still
    answers out loud on the same call.
    """

    text: str


class RawPCMSerializer(FrameSerializer):
    """Raw PCM both ways, plus one control message for interruptions."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, InterruptionFrame):
            # Audio the browser has already queued would otherwise keep
            # playing after Sophia has been told to stop talking.
            return json.dumps({"type": "clear"})
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)) and data:
            # An odd byte count cannot be 16 bit samples and would shift
            # every following sample by one byte, which sounds like noise.
            usable = len(data) - (len(data) % 2)
            if not usable:
                return None
            return InputAudioRawFrame(
                audio=bytes(data[:usable]),
                sample_rate=SAMPLE_RATE,
                num_channels=CHANNELS,
            )
        if isinstance(data, str):
            try:
                message = json.loads(data)
            except ValueError:
                return None
            if isinstance(message, dict) and message.get("type") == "text":
                text = " ".join(str(message.get("text") or "").split())[:MAX_TYPED_LENGTH]
                if text:
                    return TypedTextFrame(text=text)
        return None
