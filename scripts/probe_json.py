#!/usr/bin/env python3
"""Inspect a JSON response file: print scalar leaf paths, flag interesting keys."""
import json
import sys

KEYS = ("task", "id", "status", "state", "url", "video", "msg", "error", "code", "data")


def walk(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            walk(v, f"{prefix}[{i}]")
    else:
        s = str(obj)
        flag = " *" if any(k in prefix.lower() for k in KEYS) else ""
        if len(s) > 200 or flag:
            print(f"{prefix} = {s[:200]}{'...' if len(s) > 200 else ''}{flag}")


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    walk(data)
