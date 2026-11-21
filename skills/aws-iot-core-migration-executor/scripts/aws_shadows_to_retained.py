#!/usr/bin/env python3
"""Converts AWS IoT Device Shadows into retained messages for ConnectMQ.

ConnectMQ has no shadow service. The usual replacement is two retained state topics per thing (and per named shadow):

  things/<thing>/shadow/reported                    device publishes its state, retained
  things/<thing>/shadow/desired                     backend publishes the target state, retained; device subscribes
  things/<thing>/shadow/name/<shadow>/reported      named shadows
  things/<thing>/shadow/name/<shadow>/desired

This script writes the import file for copy_retained.py ([{"topic", "payload_b64", "qos"}]).

Input, either:
  --dir DIR     a folder of `aws iot-data get-thing-shadow` outputs named <thing>.json (classic shadow) or
                <thing>__<shadow>.json (named shadow)
  --fetch       call the AWS CLI for every thing in --things (one name per line, or "-" for all things via
                `aws iot list-things`), saving the raw shadows to --save-dir as well

Examples:
  python aws_shadows_to_retained.py --dir shadows/ -o migration/shadows-retained.json
  python aws_shadows_to_retained.py --fetch --things things.txt --endpoint-url https://xxxx-ats.iot.eu-west-1.amazonaws.com \\
      --save-dir shadows/ -o migration/shadows-retained.json
  python copy_retained.py --import migration/shadows-retained.json --target mqtts://BROKER_HOST:8883 --dry-run

Options: --prefix (default "things/"), --wrap to publish {"state": ..., "version": n, "timestamp": t} instead of the
bare state, --client-map to put the new ConnectMQ client IDs in topics instead of thing names (only when the new
topic tree uses them; the default keeps thing names, matching aws_policy_to_connectmq.py --client-map).
The shadow "delta" is not copied: devices compute it from desired and reported.
Standard library only (the AWS CLI is needed for --fetch).
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import subprocess
import sys
import tempfile


def aws(args: list[str], profile: str | None, region: str | None) -> dict:
    cmd = ["aws", *args, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    if region:
        cmd += ["--region", region]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"aws {' '.join(args)} failed")
    return json.loads(res.stdout) if res.stdout.strip() else {}


def list_all_things(profile, region) -> list[str]:
    names, token = [], None
    while True:
        args = ["iot", "list-things", "--max-results", "250"]
        if token:
            args += ["--next-token", token]
        page = aws(args, profile, region)
        names += [t["thingName"] for t in page.get("things", [])]
        token = page.get("nextToken")
        if not token:
            return names


def fetch(args) -> dict[tuple[str, str | None], dict]:
    if args.things == "-":
        things = list_all_things(args.profile, args.region)
    else:
        with open(args.things, encoding="utf-8") as f:
            things = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    ep = ["--endpoint-url", args.endpoint_url] if args.endpoint_url else []
    shadows: dict[tuple[str, str | None], dict] = {}
    for thing in things:
        names: list[str | None] = [None]
        try:
            names += aws(["iot-data", "list-named-shadows-for-thing", "--thing-name", thing, *ep],
                         args.profile, args.region).get("results", [])
        except RuntimeError as e:
            print(f"{thing}: cannot list named shadows ({e})", file=sys.stderr)
        for name in names:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                out = tmp.name
            cmd = ["aws", "iot-data", "get-thing-shadow", "--thing-name", thing, *ep]
            if name:
                cmd += ["--shadow-name", name]
            if args.profile:
                cmd += ["--profile", args.profile]
            if args.region:
                cmd += ["--region", args.region]
            res = subprocess.run(cmd + [out], capture_output=True, text=True)
            if res.returncode != 0:
                if "ResourceNotFoundException" not in res.stderr:
                    print(f"{thing}/{name or 'classic'}: {res.stderr.strip()}", file=sys.stderr)
                os.unlink(out)
                continue
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
            os.unlink(out)
            shadows[(thing, name)] = doc
            if args.save_dir:
                os.makedirs(args.save_dir, exist_ok=True)
                fn = f"{thing}__{name}.json" if name else f"{thing}.json"
                with open(os.path.join(args.save_dir, fn), "w", encoding="utf-8") as f:
                    json.dump(doc, f, indent=1)
    return shadows


def read_dir(path: str) -> dict[tuple[str, str | None], dict]:
    shadows = {}
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json"):
            continue
        base = fn[:-5]
        thing, _, name = base.partition("__")
        with open(os.path.join(path, fn), encoding="utf-8-sig") as f:
            shadows[(thing, name or None)] = json.load(f)
    return shadows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="Folder of get-thing-shadow JSON outputs")
    src.add_argument("--fetch", action="store_true", help="Read shadows with the AWS CLI")
    ap.add_argument("--things", default="-", help="File with thing names, or '-' for all things (with --fetch)")
    ap.add_argument("--endpoint-url", help="https://<prefix>-ats.iot.<region>.amazonaws.com (recommended with --fetch)")
    ap.add_argument("--profile")
    ap.add_argument("--region")
    ap.add_argument("--save-dir", help="With --fetch: also save the raw shadow documents here")
    ap.add_argument("--prefix", default="things/")
    ap.add_argument("--wrap", action="store_true", help="Publish state + version + timestamp instead of bare state")
    ap.add_argument("--client-map", help="CSV old_client_id,new_client_id: use new IDs in topics instead of thing names")
    ap.add_argument("--qos", type=int, choices=(0, 1), default=1)
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    rename = {}
    if args.client_map:
        with open(args.client_map, newline="", encoding="utf-8-sig") as f:
            rename = {r["old_client_id"]: r["new_client_id"] for r in csv.DictReader(f)}

    shadows = fetch(args) if args.fetch else read_dir(args.dir)
    items, skipped = [], 0
    for (thing, name), doc in sorted(shadows.items(), key=lambda x: (x[0][0], x[0][1] or "")):
        state = doc.get("state", {})
        base = f"{args.prefix}{rename.get(thing, thing)}/shadow" + (f"/name/{name}" if name else "")
        for part in ("reported", "desired"):
            value = state.get(part)
            if value is None:
                continue
            if value == {}:
                skipped += 1
                continue
            payload = {"state": value, "version": doc.get("version"), "timestamp": doc.get("timestamp")} \
                if args.wrap else value
            data = json.dumps(payload, separators=(",", ":")).encode()
            items.append({"topic": f"{base}/{part}", "payload_b64": base64.b64encode(data).decode(),
                          "qos": args.qos})
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)
    sizes = [len(base64.b64decode(i["payload_b64"])) for i in items]
    print(f"{len(shadows)} shadows -> {len(items)} retained messages ({skipped} empty skipped)"
          + (f", largest {max(sizes)} bytes" if sizes else "") + f"; wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
