#!/usr/bin/env python3
"""Converts AWS IoT Core policies to ConnectMQ access control rules.

Input: one or more policy files, either the raw policy document or the output of
`aws iot get-policy --policy-name NAME` (whose "policyDocument" is a JSON string).

  aws iot get-policy --policy-name DevicePolicy > policies/DevicePolicy.json
  python aws_policy_to_connectmq.py policies/*.json --out migration/
  python aws_policy_to_connectmq.py policies/*.json --client-map migration/client-map.csv --out migration/
  python aws_policy_to_connectmq.py policies/BackendPolicy.json --subject username:k3x9q2p7 --out migration/backend

Mapping:
  iot:Publish   + topic/<t>        -> publish rule on <t>
  iot:Subscribe + topicfilter/<f>  -> subscribe rule on <f>
  iot:Receive   + topic/<t>        -> no ConnectMQ equivalent (subscribe rules decide delivery); reported in notes
  iot:Connect   + client/<id>      -> no rule; ConnectMQ enforces the issued client ID prefix; reported in notes
  iot:*                            -> publish + subscribe
  Deny statements                  -> deny rules placed first (an explicit Deny always wins in AWS)

Policy variables:
  ${iot:ClientId}, ${iot:Connection.Thing.ThingName} -> ${clientid}, or one rule per thing when --client-map is
  given (old ID kept in the topic, new ConnectMQ client ID as subject). Other variables (thing attributes,
  certificate fields) cannot be mapped; those statements are skipped and listed.

Wildcards in resource ARNs are policy wildcards, not MQTT wildcards: '*' at the end of a resource -> '#',
'*' or '?' inside a level -> '+' (with a warning, because AWS '*' also spans levels).

Reserved topics: $aws/things/<thing>/shadow/... -> <shadow-prefix><thing>/shadow/... (default "things/"),
$aws/rules/<rule>/<topic> (Basic Ingest) -> <topic>; other $aws/ topics (jobs, events, defender, provisioning)
are skipped and listed. --remap OLD=NEW rewrites any other topic prefix.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectmq_acl import Rule, check, dedupe, expand_pattern, load_map, render  # noqa: E402

CLIENT_VARS = re.compile(r"\$\{iot:(ClientId|Connection\.Thing\.ThingName)\}")
ANY_VAR = re.compile(r"\$\{[^}]+\}")
ACTION_NAMES = {"iot:publish": "iot:Publish", "iot:subscribe": "iot:Subscribe"}
ARN = re.compile(r"^arn:aws[a-z-]*:iot:[^:]*:[^:]*:(topic|topicfilter|client|thing|thinggroup)/(.*)$")


def load_policy(path: str) -> tuple[str, dict]:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    name = data.get("policyName") or os.path.splitext(os.path.basename(path))[0]
    doc = data.get("policyDocument", data)
    if isinstance(doc, str):
        doc = json.loads(doc)
    return name, doc


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def convert_wildcards(topic: str, warnings: list[str]) -> str:
    levels = topic.split("/")
    out = []
    for i, level in enumerate(levels):
        last = i == len(levels) - 1
        if "*" in level or "?" in level:
            if last and level.endswith("*") and level == "*":
                out.append("#")
            elif last and "*" in level:
                out.append("#")
                warnings.append(f"'{level}' (policy wildcard) broadened to '#'")
            else:
                out.append("+")
                warnings.append(f"'{level}' (policy wildcard) mapped to '+'; AWS '*' can also span levels")
            if last or out[-1] == "#":
                break
        else:
            out.append(level)
    return "/".join(out)


def remap_topic(topic: str, shadow_prefix: str, remaps: list[tuple[str, str]], notes: list[str],
                warnings: list[str]) -> str | None:
    if topic.startswith("$aws/things/"):
        rest = topic[len("$aws/things/"):]
        thing, _, tail = rest.partition("/")
        if tail.startswith("shadow"):
            notes.append(f"Shadow topics are mapped 1:1 under '{shadow_prefix}<thing>/shadow/...'. If devices adopt the "
                         f"retained design ({shadow_prefix}<thing>/shadow/reported and /desired), replace these rules "
                         f"with publish '{shadow_prefix}${{clientid}}/shadow/reported' and subscribe "
                         f"'{shadow_prefix}${{clientid}}/shadow/desired' (or per-thing equivalents).")
            return f"{shadow_prefix}{thing}/{tail}"
        notes.append(f"Skipped reserved topic '{topic}' (AWS IoT Jobs/other thing service): no ConnectMQ equivalent.")
        return None
    if topic.startswith("$aws/rules/"):
        parts = topic.split("/", 3)
        if len(parts) == 4:
            warnings.append(f"Basic Ingest topic '{topic}' mapped to '{parts[3]}'; a backend subscriber replaces "
                            f"rule '{parts[2]}'")
            return parts[3]
        return None
    if topic.startswith("$aws/") or topic == "$aws":
        notes.append(f"Skipped reserved topic '{topic}': AWS service topics (events, defender, provisioning, "
                     "jobs) have no ConnectMQ equivalent.")
        return None
    for old, new in remaps:
        if topic.startswith(old):
            return new + topic[len(old):]
    return topic


def convert(args) -> tuple[list[Rule], list[str]]:
    client_map = load_map(args.client_map, "old_client_id", "new_client_id")
    remaps = []
    for r in args.remap:
        if "=" not in r:
            raise SystemExit(f"--remap {r}: use OLD=NEW")
        old, new = r.split("=", 1)
        remaps.append((old, new))
    subj_type, subj_value = "all", ""
    if args.subject:
        subj_type, _, subj_value = args.subject.partition(":")
        if subj_type not in ("username", "clientid"):
            raise SystemExit("--subject must be username:<value> or clientid:<glob>")

    notes: list[str] = []
    deny, allow = [], []
    for path in args.policies:
        name, doc = load_policy(path)
        for n, st in enumerate(as_list(doc.get("Statement")), 1):
            where = f"{name} statement {n}"
            if "NotAction" in st or "NotResource" in st:
                notes.append(f"{where}: NotAction/NotResource cannot be converted; review manually.")
                continue
            if "Condition" in st:
                notes.append(f"{where}: Condition {json.dumps(st['Condition'])} ignored (ConnectMQ rules have no "
                             "conditions); the converted rule is broader than the original.")
            effect = st.get("Effect", "Allow").lower()
            perm = "deny" if effect == "deny" else "allow"
            actions = [a.lower() for a in as_list(st.get("Action"))]
            if any(a in ("iot:*", "*") for a in actions):
                actions = ["iot:publish", "iot:subscribe", "iot:receive", "iot:connect"]
            for res in as_list(st.get("Resource")):
                if res == "*":
                    kind_topic = {"iot:publish": ("topic", "*"), "iot:subscribe": ("topicfilter", "*"),
                                  "iot:receive": ("topic", "*"), "iot:connect": ("client", "*")}
                    pairs = [(a, *kind_topic[a]) for a in actions if a in kind_topic]
                else:
                    m = ARN.match(res)
                    if not m:
                        notes.append(f"{where}: resource '{res}' not recognised; skipped.")
                        continue
                    pairs = [(a, m.group(1), m.group(2)) for a in actions]
                for action, kind, value in pairs:
                    if action == "iot:connect":
                        if kind == "client":
                            notes.append(f"{where}: iot:Connect on client/{value} - ConnectMQ enforces client IDs "
                                         "through the issued ID prefix (<ISSUED_ID>_<suffix>); no rule needed.")
                        continue
                    if action == "iot:receive":
                        notes.append(f"{where}: iot:Receive on {value} has no equivalent; delivery is controlled by "
                                     "subscribe rules on ConnectMQ.")
                        continue
                    want = {"iot:publish": "topic", "iot:subscribe": "topicfilter"}.get(action)
                    if want is None:
                        notes.append(f"{where}: action {action} has no ConnectMQ equivalent; skipped.")
                        continue
                    if kind != want:
                        # e.g. Subscribe on topic/... never matches in AWS; report and skip
                        notes.append(f"{where}: {action} on {kind}/{value} never matches in AWS (needs {want}/); "
                                     "skipped.")
                        continue
                    warnings: list[str] = []
                    topic = CLIENT_VARS.sub("${clientid}", value)
                    if ANY_VAR.sub("", topic.replace("${clientid}", "")) != topic.replace("${clientid}", ""):
                        notes.append(f"{where}: '{value}' uses a policy variable other than ClientId/ThingName; "
                                     "skipped - add explicit rules.")
                        continue
                    topic = convert_wildcards(topic, warnings)
                    topic = remap_topic(topic, args.shadow_prefix, remaps, notes, warnings)
                    if topic is None:
                        continue
                    rule = Rule(perm, "publish" if action == "iot:publish" else "subscribe", topic, subj_type,
                                subj_value, f"{where}: {ACTION_NAMES[action]} {value}", warnings)
                    rules = [rule]
                    if "${clientid}" in topic:
                        if client_map:
                            rules = expand_pattern(rule, client_map, "${clientid}")
                            for r in rules:
                                r.warnings = list(warnings)
                        else:
                            rule.warnings.append("${clientid} is now the full ConnectMQ ID (<ISSUED>_<thing>); "
                                                 "pass --client-map to keep thing names in topics")
                    (deny if perm == "deny" else allow).extend(rules)
    # Merge publish+subscribe on the same filter/subject into 'all'
    merged = []
    for group in (deny, allow):
        keyed: dict[tuple, Rule] = {}
        for r in group:
            k = (r.permission, r.topic_filter, r.subject_type, r.subject_value)
            if k in keyed and keyed[k].action != r.action:
                keyed[k].action = "all"
                keyed[k].description += " + " + r.description
            elif k not in keyed:
                keyed[k] = r
        merged += list(keyed.values())
    return dedupe(merged), list(dict.fromkeys(notes))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("policies", nargs="+", help="Policy JSON files (raw document or `aws iot get-policy` output)")
    ap.add_argument("--client-map", help="CSV old_client_id,new_client_id (old = thing name / AWS client ID)")
    ap.add_argument("--subject", help="Subject for all rules: username:<value> or clientid:<glob> (default all)")
    ap.add_argument("--shadow-prefix", default="things/", help="Prefix replacing $aws/things/ (default things/)")
    ap.add_argument("--remap", action="append", default=[], help="Topic prefix rewrite OLD=NEW (repeatable)")
    ap.add_argument("--out", default="migration", help="Output directory (default ./migration)")
    args = ap.parse_args()

    rules, notes = convert(args)
    problems = check(rules)
    os.makedirs(args.out, exist_ok=True)
    for fmt, ext in (("markdown", "md"), ("csv", "csv"), ("json", "json")):
        with open(os.path.join(args.out, f"acl-rules.{ext}"), "w", encoding="utf-8", newline="") as f:
            f.write(render(rules, "deny", fmt, notes + problems))
    print(f"wrote {args.out}/acl-rules.md: {len(rules)} rules, {len(notes)} notes, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
