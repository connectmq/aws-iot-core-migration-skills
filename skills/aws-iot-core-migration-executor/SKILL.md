---
name: aws-iot-core-migration-executor
description: Executes a planned migration from AWS IoT Core to the ConnectMQ managed MQTT broker step by step. Verifies the ConnectMQ broker, sets up a least-privilege relay thing and relay.py between AWS IoT Core and ConnectMQ, copies Device Shadow state into retained topics, validates message flow, moves devices in waves via firmware or AWS IoT Jobs with go/no-go checks, rolls back, and decommissions certificates and topic rules, keeping a migration log. Use when an AWS-IoT-Core-to-ConnectMQ plan exists and the team is ready to run the relay, migrate shadows, switch a device group, cut over, roll back or troubleshoot connection errors.
license: MIT
metadata:
  source-broker: aws-iot-core
  target-broker: connectmq
  companion-skill: aws-iot-core-migration-planner
---

# AWS IoT Core to ConnectMQ: Migration Executor

You carry out an AWS IoT Core to ConnectMQ migration one verified step at a time. You prepare configuration, run the
scripts and AWS CLI commands, read the results, and stop for a human decision at every go/no-go point.

Before starting, read:

- `migration/plan.md`, `migration/topic-map.csv`, `migration/client-map.csv`, `migration/acl-rules.md` from the
  planner. If they do not exist, offer to run the **aws-iot-core-migration-planner** skill first.
- `references/connectmq-target.md` for limits and reason codes.
- `references/aws-iot-core-bridge.md` for the relay thing, relay setup, shadows and decommissioning.
- `references/cutover-and-rollback.md` for wave rules, go/no-go and rollback.

## Rules

- **Ask before every production change**: creating AWS resources, ACL changes, starting a job/OTA, disabling a rule,
  deactivating certificates. State what changes and how to undo it.
- Read-only AWS CLI commands are fine to run when the user has given access; never delete AWS resources before the
  rollback window has passed and the user confirms.
- Secrets (relay private key, ConnectMQ passwords) stay out of committed files: environment variables or a secrets
  manager.
- Append every step to `migration/log.md`: time, step, command, result, numbers, decision.
- On a failing check: stop, diagnose with the reason-code table, propose a fix or rollback.

## Scripts

In `scripts/` (Python 3.9+, `pip install "paho-mqtt>=2.0"`; `aws_shadows_to_retained.py` uses only the standard
library plus the AWS CLI for `--fetch`):

| Script | Purpose |
| --- | --- |
| `verify_connectmq.py` | Connect, QoS 0/1/2, retained, will, session, ACL deny checks |
| `relay.py` | AWS IoT Core (X.509) <-> ConnectMQ relay with loop protection |
| `aws_shadows_to_retained.py` | Device Shadows (classic + named) -> retained-message import file |
| `copy_retained.py` | Publishes the import file (or copies retained messages) to ConnectMQ |
| `compare_streams.py` | Subscribes on both brokers and compares topics, counts and payloads |
| `client_id_map.py` | Thing names -> `<ISSUED_ID>_<thing>` |

## Phase 1: Prepare ConnectMQ

1. Confirm the plan's ConnectMQ plan in the console (connections, retained limit, ACL available).
2. Create credentials per the plan, plus the relay credential and a test credential.
3. Enter ACL rules from `migration/acl-rules.md` (relay rules first), set no-match to deny, then enable ACL.
4. Smoke test: `python scripts/verify_connectmq.py --url mqtts://BROKER_HOST:8883 --prefix <test branch>` plus
   `--expect-deny-publish` for a topic devices must not write.

## Phase 2: Relay and state

1. Create the relay thing, certificate and least-privilege policy (`references/aws-iot-core-bridge.md` section 1).
   Show the policy to the user before creating it.
2. Start `relay.py` with the forward/reverse filters from the plan; confirm both sides connected and counters rise.
3. Copy shadow state:

   ```bash
   python scripts/aws_shadows_to_retained.py --fetch --things migration/things.txt \
     --endpoint-url https://<prefix>-ats.iot.<region>.amazonaws.com --save-dir migration/aws/shadows \
     -o migration/shadows-retained.json
   python scripts/copy_retained.py --import migration/shadows-retained.json --target mqtts://BROKER_HOST:8883 \
     --limit <plan retained limit> --dry-run
   ```

   Then run without `--dry-run`. Schedule it while devices are split (section 4 of the bridge reference).
4. Validate: `python scripts/compare_streams.py --source mqtts://<ats-endpoint>:8883 --source-cafile AmazonRootCA1.pem
   --source-cert relay-check.cert.pem --source-key relay-check.private.key --source-client-id migration-check
   --target mqtts://BROKER_HOST:8883 --filter 'dt/#' --duration 600`. Use a separate "check" thing whose policy
   allows `iot:Connect` on `client/migration-check`, `iot:Subscribe` on the compared filters and
   `iot:Receive` on their topics; never reuse the relay's client ID, or the two take over each other's session.
   Expect MATCH.
5. Move backends (rule replacements, shared-subscription consumers) to ConnectMQ in shadow mode; compare their
   output with the AWS rules' output for at least 24 hours before switching the rules off.

## Phase 3: Device waves

1. Confirm firmware step 1 (ConnectMQ root CA + credential storage) is on every device of the wave.
2. Go/no-go checklist from `references/cutover-and-rollback.md`; explicit "go" from the user.
3. Trigger step 2 for the wave's thing group (OTA or AWS IoT Job). Show the exact job document / config diff:

   ```text
   endpoint: <prefix>-ats.iot.<region>.amazonaws.com -> BROKER_HOST
   port: 8883 / 443 ALPN                         -> 8883 (or wss 8084/mqtt)
   auth: X.509 device certificate                -> username/password (device's own credential)
   root CA: Amazon Root CA 1                      -> ConnectMQ public CA chain
   client_id: pump-001                            -> <ISSUED_ID>_pump-001
   topics: $aws/things/pump-001/shadow/update     -> things/pump-001/shadow/reported (retained)
   keepalive: 1200                                -> <= 600
   ```

4. Watch 15 minutes: ConnectMQ connected count rises by the wave size, AWS `Connect.Success` for the group falls,
   no `0x85/0x86/0x87/0x8A/0x97`, `compare_streams.py` still MATCH for the remaining AWS devices.
5. Soak, log numbers, then propose the next wave. On a rollback trigger, revert the wave's config (the AWS
   certificates are still active) and log the cause.

## Phase 4: Cutover and decommission

1. All waves soaked: stop scheduled shadow copies, stop the relay.
2. Disable (not delete) the topic rules; deactivate device certificates per thing group; watch for stragglers in
   CloudWatch `Connect.AuthError`.
3. After the rollback window and with user confirmation: delete certificates, rules, policies and the relay thing
   (`references/aws-iot-core-bridge.md` section 7). Keep the thing registry if other AWS services use it.
4. Update provisioning so new devices get ConnectMQ credentials instead of AWS certificates.

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Relay: AWS connect fails (TLS / "connection lost" right after connect) | Policy `iot:Connect` resource does not match `--source-client-id`, certificate inactive or policy not attached | Fix client ID or policy; `aws iot describe-certificate` |
| Relay: AWS subscribe succeeds but nothing arrives | Missing `iot:Receive` for the topics | Add `topic/dt/*` Receive |
| Relay: reverse commands never reach AWS devices | Missing `iot:Publish` on `topic/cmd/*`, or ConnectMQ ACL denies relay subscribe | Fix policy / ACL |
| Device: TLS handshake fails against ConnectMQ | Firmware only trusts Amazon Root CA | Firmware step 1 (add the ConnectMQ root CA) |
| Device: CONNACK `0x85` | Thing name used as client ID without `<ISSUED_ID>_` prefix | Use `client-map.csv` |
| Device: CONNACK `0x86` / `0x8A` | Wrong credential, retried in a loop (IP ban) | Stop the wave, fix delivery of credentials, add backoff |
| Device: SUBACK `0x87` on `things/...` | ACL still has 1:1 shadow API rules, not the retained `reported/desired` design | Update rules to the topic map |
| Backend misses data | A topic rule had no replacement | Check `list-topic-rules` against the plan's replacement list |
| `0x97` on publish | Fast publisher above the plan's per-client rate | Batch, slow down, or upgrade |
