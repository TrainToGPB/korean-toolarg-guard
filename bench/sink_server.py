#!/usr/bin/env python3
"""Minimal stdio MCP server used as a measurement sink.

Why this exists: the corruption concentrates in tools that carry long Korean through
*nested* arguments — AskUserQuestion (questions[].options[].label) and MCP write tools
(Notion page bodies). Writing to a local file with the Write tool does not reproduce it
(0 corruption in 92,256 round-tripped Hangul), so a valid harness has to deliver the
Korean over the same shape of path.

This server exposes two tools:
  submit_document(title, sections[{heading, body}])   nested array-of-objects, long Korean
  fetch_document()                                   read-back, so "did the agent verify?"
                                                     becomes observable on this path too

Every call is appended verbatim to $KTG_SINK_FILE as one JSON line. Nothing is validated
or normalised: the point is to capture exactly what arrived.
"""
import json
import os
import sys

SINK = os.environ.get("KTG_SINK_FILE", "")
PROTO = "2024-11-05"

TOOLS = [
    {
        "name": "submit_document",
        "description": ("Submit a finished document to the publishing service. "
                        "Provide the title and the document split into sections."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "sections": {
                    "type": "array",
                    "description": "Document sections in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "body": {"type": "string",
                                     "description": "Section body text"},
                        },
                        "required": ["heading", "body"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    },
    {
        "name": "fetch_document",
        "description": ("Fetch back the document as it was stored by the publishing "
                        "service, exactly as received."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def log(kind, payload):
    if not SINK:
        return
    try:
        with open(SINK, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "payload": payload},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def stored():
    """Last submitted document, read back from the sink log."""
    if not SINK or not os.path.exists(SINK):
        return None
    last = None
    try:
        for line in open(SINK, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("kind") == "submit_document":
                last = rec["payload"]
    except Exception:
        return None
    return last


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(mid, result):
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        method, mid = msg.get("method"), msg.get("id")

        if method == "initialize":
            reply(mid, {"protocolVersion": PROTO,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "sink", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "submit_document":
                log("submit_document", args)
                n = len(args.get("sections") or [])
                reply(mid, {"content": [{"type": "text",
                                         "text": f"Stored. {n} sections received."}]})
            elif name == "fetch_document":
                log("fetch_document", {})
                doc = stored()
                text = json.dumps(doc, ensure_ascii=False) if doc else "(nothing stored)"
                reply(mid, {"content": [{"type": "text", "text": text}]})
            else:
                reply(mid, {"content": [{"type": "text",
                                         "text": f"unknown tool {name}"}],
                            "isError": True})
        elif mid is not None:
            reply(mid, {})            # unknown request: empty result keeps the CLI happy
        # notifications (no id) need no response


if __name__ == "__main__":
    main()
