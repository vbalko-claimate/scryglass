"""Parse MTGA Player.log messages into structured data."""
import json
import re
from typing import Generator

# Header pattern: [UnityCrossThreadLogger]TIMESTAMP: Direction: MessageType
# Direction: "Match to PLAYERID" (incoming) or "PLAYERID to Match" (outgoing)
HEADER_RE = re.compile(
    r"^\[UnityCrossThreadLogger\]"
    r"(\d+/\d+/\d+ \d+:\d+:\d+ [AP]M): "
    r"(Match to (\w+)|(\w+) to Match): "
    r"(\w+)$"
)

# Message types we care about
IMPORTANT_TYPES = {
    "GreToClientEvent",
    "MatchGameRoomStateChangedEvent",
    "ClientToGremessage",
    "AuthenticateResponse",
}


def parse_message(header_line: str, json_lines: list[str]) -> dict | None:
    """Parse a single log message (header + JSON payload)."""
    m = HEADER_RE.match(header_line)
    if not m:
        return None

    timestamp = m.group(1)
    player_id = m.group(3) or m.group(4)
    direction = "incoming" if m.group(3) else "outgoing"
    msg_type = m.group(5)

    if msg_type not in IMPORTANT_TYPES:
        return None

    # Join JSON lines and parse
    json_text = "".join(json_lines).strip()
    if not json_text:
        return None

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return None

    return {
        "timestamp": timestamp,
        "player_id": player_id,
        "direction": direction,
        "type": msg_type,
        "payload": payload,
    }


def extract_gre_messages(msg: dict) -> list[dict]:
    """Extract individual GRE messages from a GreToClientEvent."""
    if msg["type"] != "GreToClientEvent":
        return [msg]

    payload = msg["payload"]
    gre_event = payload.get("greToClientEvent", {})
    gre_messages = gre_event.get("greToClientMessages", [])

    results = []
    for gm in gre_messages:
        results.append({
            "timestamp": msg["timestamp"],
            "player_id": msg["player_id"],
            "direction": msg["direction"],
            "type": gm.get("type", "unknown"),
            "gre_msg": gm,
        })
    return results


def iter_messages_from_lines(lines: list[str]) -> Generator[dict, None, None]:
    """Parse a batch of log lines and yield structured messages."""
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n\r")

        # Try to match a header
        m = HEADER_RE.match(line)
        if m:
            msg_type = m.group(5)
            if msg_type in IMPORTANT_TYPES:
                # Collect JSON payload (may be on same line continuation or next lines)
                json_lines = []
                i += 1

                # JSON could start on the next line
                if i < len(lines):
                    next_line = lines[i].rstrip("\n\r")
                    if next_line.startswith("{"):
                        # Single-line JSON (common for GreToClientEvent)
                        if next_line.rstrip().endswith("}"):
                            json_lines.append(next_line)
                            i += 1
                        else:
                            # Multi-line JSON (common for ClientToGremessage)
                            brace_count = next_line.count("{") - next_line.count("}")
                            json_lines.append(next_line)
                            i += 1
                            while i < len(lines) and brace_count > 0:
                                jl = lines[i].rstrip("\n\r")
                                brace_count += jl.count("{") - jl.count("}")
                                json_lines.append(jl)
                                i += 1

                parsed = parse_message(line, json_lines)
                if parsed:
                    yield parsed
            else:
                i += 1
        else:
            i += 1
