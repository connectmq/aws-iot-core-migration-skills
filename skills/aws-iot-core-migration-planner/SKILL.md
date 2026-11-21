---
name: aws-iot-core-migration-planner
description: Plans a migration from AWS IoT Core to the ConnectMQ managed MQTT broker. Inventories things, certificates, IoT policies, topic rules, Device Shadows and Jobs with the AWS CLI, sizes the workload from CloudWatch, checks it against ConnectMQ plan limits, converts IoT policies to ConnectMQ ACL rules, redesigns reserved $aws topics, replaces the rules engine and shadows, maps thing names to client IDs and writes a phased plan with waves and rollback. Use when someone wants to leave AWS IoT Core, cut AWS IoT costs, move devices from X.509 certificates to a managed MQTT broker, or asks how IoT policies, shadows or rules translate to ConnectMQ.
license: MIT
metadata:
  source-broker: aws-iot-core
  target-broker: connectmq
  companion-skill: aws-iot-core-migration-executor
---

# AWS IoT Core to ConnectMQ: Migration Planner

You are an IoT migration engineer. You produce a plan for moving devices and backends from AWS IoT Core to
ConnectMQ that can be executed without losing data or bricking devices. You produce files: inventory, converted ACL
rules, client ID map, topic map and a filled-in plan.

Read `references/connectmq-target.md` first: generated credentials, the issued client ID prefix, no client
certificates and the plan limits drive the plan. `references/aws-iot-core-mapping.md` maps every AWS IoT service and
lists AWS-specific pitfalls.

## Principles

- **AWS IoT Core is several services.** Inventory the broker usage *and* the rules, shadows, jobs and provisioning
  around it. Anything without a replacement owner is a blocker.
- **Device firmware is the critical path.** Moving from X.509 to username/password and from Amazon Root CA to
  ConnectMQ's CA needs a firmware or config update on every device. Plan OTA capacity and a two-step rollout
  (step 1: add the new CA and credentials storage; step 2: switch endpoint).
- **Relay and migrate.** AWS IoT Core cannot bridge out natively, so the parallel run uses `relay.py` (a dedicated
  relay thing with its own certificate) between AWS IoT Core and ConnectMQ.
- **Redesign `$aws/` topics once.** Shadows, jobs, basic ingest and lifecycle events move to ordinary topics; agree
  on the new topic tree before the first device update, because changing it later means another OTA.

## Workflow

### Step 1: Collect the facts

Ask for, and do not proceed on assumptions for the starred items:

- AWS account(s) and Region(s), and whether the user can run read-only AWS CLI commands *
- Device firmware/SDK (AWS IoT Device SDK, FreeRTOS coreMQTT, ESP-IDF, Greengrass, custom) and how it is updated
  (AWS IoT Jobs OTA, own OTA, manual) *
- Where device credentials live (secure element, flash) and whether the root CA can be updated *
- Client groups: backends (Lambda, containers), apps (Cognito/WebSocket), gateways, devices; downtime tolerance
- Topic rules and downstream services that must keep working (DynamoDB, Timestream, Kinesis, S3, Lambda)
- Planned date range; whether AWS stays for other services (data lake, Lambda) or is removed completely

### Step 2: Inventory

Run the commands in `references/aws-iot-core-mapping.md` ("Inventory commands") and save outputs under
`migration/aws/`: things, groups, policies (one `get-policy` JSON per policy), policy targets, topic rules, a
shadow sample, logging options, Jobs usage. Summarise:

- Number of things, certificates (active), policies, rules, things with shadows (classic/named), Jobs in use.
- Topics actually used (from policies, rules' `FROM` clauses and firmware), including every `$aws/` topic.
- Every rule: SQL, actions, owner of the replacement.

### Step 3: Measure the workload

From CloudWatch (`AWS/IoT`, 30 days, hourly sums): `Connect.Success`, `PublishIn.Success`, `PublishOut.Success`,
`Subscribe.Success`. Estimate concurrent connections from device behaviour (always-on vs periodic) or lifecycle
events. Get the largest payload and the fastest publisher from firmware specs or a temporary test subscriber.

### Step 4: Fit check and plan choice

Fill section 2 of `assets/migration-plan-template.md`:

- Concurrent connections = peak connected devices + relay (1) + backends + apps + 20 % headroom.
- Session minutes per month = connected devices x connected minutes (always on: about 43,200).
- Messages per second per client vs plan limit (AWS allows ~100/s per connection; check the fastest publisher).
- Shadows to retained messages: count of things x (reported + desired) x named shadows vs plan retained limit.
- Session expiry vs longest offline period.
- ACL (policies) and shared subscriptions (rule replacements) -> Basic or Scale.
- Above Scale -> contact support@connectmq.com for a custom plan first.

### Step 5: Topic redesign

Write `migration/topic-map.csv` (`aws_topic,connectmq_topic,direction,owner`). Defaults:

| AWS topic | ConnectMQ topic |
| --- | --- |
| `$aws/things/<t>/shadow/update` (reported) | `things/<t>/shadow/reported` (retained) |
| `$aws/things/<t>/shadow/update` (desired, from backend) | `things/<t>/shadow/desired` (retained) |
| `$aws/things/<t>/shadow/update/delta` | device compares `desired` with its state |
| `$aws/things/<t>/shadow/name/<n>/...` | `things/<t>/shadow/name/<n>/{reported,desired}` |
| `$aws/rules/<rule>/<topic>` | `<topic>` |
| `$aws/things/<t>/jobs/...` | `things/<t>/jobs/...` (own job service) |
| `$aws/events/presence/...` | `things/<t>/status` (Last Will + retained online) |
| application topics (`dt/...`, `cmd/...`) | unchanged |

### Step 6: Identities and ACL

1. Choose credential scope (per device recommended for device fleets coming from per-device certificates; per site
   or group if the plan's credential management is too heavy). Explain revocation trade-offs.
2. Thing names to client IDs:
   `aws iot list-things --query 'things[].thingName' --output text | tr '\t' '\n' > migration/things.txt`
   `python scripts/client_id_map.py migration/things.txt --issued-id <ISSUED_ID> -o migration/client-map.csv`
3. Convert policies:
   `python scripts/aws_policy_to_connectmq.py migration/aws/policies/*.json --client-map migration/client-map.csv --out migration/`
   Backend/admin policies: convert separately with `--subject username:<backend credential>`. Review every note
   (conditions, `iot:Receive`, thing-attribute variables, skipped `$aws/` topics) with the user; replace mapped
   shadow API rules with the retained `reported/desired` rules once the topic map is agreed.

### Step 7: Replacements

For every topic rule and AWS service in use, record in the plan: replacement (backend subscriber, retained topics,
own job service, ConnectMQ alerts), owner, and test. A shared-subscription consumer per rule group keeps the
existing AWS targets (DynamoDB, Timestream, S3) working if the data stays in AWS.

### Step 8: Write the plan

Copy `assets/migration-plan-template.md` to `migration/plan.md` and fill it. The waves for AWS IoT Core are usually:

0. Relay up, backends consuming from ConnectMQ in shadow mode.
1. Firmware step 1 (new root CA + credential storage, still on AWS) to all devices.
2. Canary devices switched to ConnectMQ (firmware step 2 or remote config), relay carries commands back for the rest.
3. Growing waves by thing group.
4. Rules disabled, relay stopped, certificates deactivated after the rollback window.

Finish with a summary: plan chosen, blockers and resolutions, firmware steps, number of waves, open questions.

## Output checklist

- [ ] `migration/aws/` inventory with policies, rules, shadow samples
- [ ] Workload numbers and plan selected (or custom plan requested)
- [ ] `migration/topic-map.csv` agreed with firmware and backend owners
- [ ] `migration/client-map.csv` without errors
- [ ] `migration/acl-rules.md` reviewed, notes resolved
- [ ] Every topic rule / AWS service has a replacement owner
- [ ] Firmware rollout (root CA, credentials, endpoint) planned with OTA capacity
- [ ] `migration/plan.md` complete with rollback per wave

Hand over to the **aws-iot-core-migration-executor** skill to carry out the plan.
