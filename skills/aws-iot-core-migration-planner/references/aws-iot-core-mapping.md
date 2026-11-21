# AWS IoT Core to ConnectMQ: Feature and Configuration Mapping

AWS IoT Core is not just a broker: it bundles device identity (things, certificates), authorization (IoT policies),
a rules engine, Device Shadow, Jobs, provisioning and monitoring. ConnectMQ is a standards-based MQTT broker with a
console, access control, dashboard and alerts. Map each AWS service to a ConnectMQ feature, a topic design or a small
backend service, and mark what disappears.

AWS quota values below were current when written; check the AWS IoT Core "Quotas" page for your account and Region.

## Connection and identity

| AWS IoT Core | ConnectMQ | Notes |
| --- | --- | --- |
| Endpoint `xxxx-ats.iot.<region>.amazonaws.com` (`aws iot describe-endpoint --endpoint-type iot:Data-ATS`) | `BROKER_HOST` from the console | Every device changes host |
| Port 8883 (MQTT/TLS), 443 with ALPN `x-amzn-mqtt-ca`, 443 WebSocket (SigV4) | 8883 (TLS), 8084 (`wss://HOST:8084/mqtt`) | No ALPN on 443; devices behind firewalls that only allow 443 need 8084 (WebSocket) or a firewall change |
| Server CA: Amazon Root CA 1 / 3 | Public CA (Let's Encrypt, ISRG Root X1 chain on `broker.connectmq.com`; check your broker host) | **Devices that embed only Amazon Root CA must get the new root CA in firmware** |
| X.509 device certificate (mTLS) | Username + password over TLS | **Blocker**: firmware/config change per device to send credentials |
| Custom authorizer (Lambda, token) | Username + password | Token logic moves out; credentials are issued in the console |
| SigV4 WebSocket (Cognito, IAM) | `wss` + username/password | Browser apps need ConnectMQ credentials from your backend; never ship a shared admin credential |
| Client ID = thing name (convention enforced by policy) | `<ISSUED_ID>_<thing-name>` | `client_id_map.py` builds the map from `list-things` |
| Thing registry, thing types, attributes, groups | not available | Keep the registry in your own database or keep using AWS only as a registry |
| Fleet Provisioning, JITP/JITR | not available | Provision credentials in your factory/onboarding process |

## Authorization

| AWS IoT Core | ConnectMQ | Notes |
| --- | --- | --- |
| IoT policy attached to certificate/thing group | Access control rules (Basic/Scale) | `aws_policy_to_connectmq.py` converts |
| `iot:Publish` on `topic/...` | `publish` rule | |
| `iot:Subscribe` on `topicfilter/...` | `subscribe` rule | ConnectMQ rule must cover the whole requested filter |
| `iot:Receive` on `topic/...` | no equivalent | Delivery follows subscribe rules |
| `iot:Connect` on `client/...` | issued client ID prefix | No rule needed |
| `${iot:ClientId}`, `${iot:Connection.Thing.ThingName}` | `${clientid}` or per-thing rules | `${clientid}` is the full new ID (`c7f3k2_pump-001`); per-thing expansion keeps thing names in topics |
| `${iot:Connection.Thing.Attributes[...]}`, certificate variables | no equivalent | Expand to explicit rules |
| Policy `*` / `?` wildcards | `#` / `+` (best effort) | AWS `*` spans levels; converted rules may be broader |
| Explicit Deny wins | Deny rules first (first match) | Converter orders them |
| `Condition` blocks | no equivalent | Converter warns; rules become broader |

## Reserved topics and services

| AWS | ConnectMQ replacement |
| --- | --- |
| Device Shadow (`$aws/things/<t>/shadow/update`, `/get`, `/delete`, `.../accepted`, `.../rejected`, `.../delta`, `.../documents`) | Retained state topics: device publishes `things/<t>/shadow/reported` (retained), backend publishes `things/<t>/shadow/desired` (retained), device subscribes to `desired` and computes the delta itself. Named shadows: `things/<t>/shadow/name/<n>/...`. No versioning or optimistic locking: put `version` in the payload if needed. `aws_shadows_to_retained.py` copies existing state |
| Basic Ingest (`$aws/rules/<rule>/<topic>`) | Publish to `<topic>` and let a backend subscriber do the rule's work |
| Rules engine (SQL `SELECT ... FROM 'topic'`) with actions: Lambda, DynamoDB, Kinesis, Firehose, S3, Timestream, SQS/SNS, republish, HTTP | A backend service subscribing with a shared subscription (`$share/ingest/dt/#`, Basic/Scale) that applies the SQL filter in code and writes to the same AWS services or their replacements. Keep the AWS targets if only the broker moves |
| Rule `republish` action | The backend publishes the derived topic |
| Jobs (`$aws/things/<t>/jobs/...`) | Your own job topics: `things/<t>/jobs/notify` (retained) and `things/<t>/jobs/<id>/status`, driven by a backend |
| Lifecycle events (`$aws/events/presence/connected/...`) | Last Will on `things/<t>/status` (`offline`) + retained `online` on connect; ConnectMQ console sessions view |
| Device Defender, Fleet Indexing / fleet search, Fleet Hub | not available; ConnectMQ dashboard, sessions view and alerts cover monitoring basics |
| AWS IoT Device Management secure tunneling | not available |
| CloudWatch metrics and logs | ConnectMQ console usage meters, sessions view, rules and alerts |

## Protocol behaviour and limits

| Topic | AWS IoT Core | ConnectMQ |
| --- | --- | --- |
| MQTT versions | 3.1.1 and 5 | 3.1.1 and 5 |
| QoS 2 | not supported (QoS 0/1 only) | supported |
| Retained messages | supported (account-level limits) | supported, count limit per plan |
| Max payload | 128 KB | 256 KB on Free/Basic (see pricing page for Scale) |
| Persistent session expiry | 1 hour default, configurable up to 7 days | Plan max: 1 h / 2 h / 24 h |
| Inbound publishes per connection | ~100/s | 20 / 50 / 100 per second by plan |
| Subscriptions per connection | 50 | 20 / 50 / 100 by plan |
| Topic levels | max 7 slashes, 256 bytes | MQTT limits (deeper trees allowed) |
| Client ID length | 128 bytes | Keep `<ISSUED_ID>_<thing>` short; thing names over ~100 chars need shortening |
| Keep-alive | 30-1200 s | capped at 600 s: devices with keep-alive > 600 must lower it |
| Shared subscriptions | `$share/<group>/<filter>` | same syntax, Basic/Scale |

## Pricing model contrast

AWS IoT Core bills per use: connectivity minutes, messages (metered in 5 KB increments), rules triggered and actions,
shadow and registry operations, plus the downstream services. ConnectMQ bills a flat monthly plan that includes a
session-minute allowance, connection and rate limits, the dashboard, access control and alerts. When building the
business case, compare a real month of AWS invoices (IoT + Lambda/Kinesis/DynamoDB used by rules) against the
ConnectMQ plan that fits the measured workload. Do not quote AWS prices from memory.

## Inventory commands

```bash
REGION=eu-west-1
aws iot describe-endpoint --endpoint-type iot:Data-ATS --region $REGION
aws iot list-things --region $REGION --query 'things[].thingName' --output text   # CLI paginates automatically
aws iot list-thing-groups --region $REGION
aws iot list-policies --region $REGION
aws iot get-policy --policy-name DevicePolicy --region $REGION > policies/DevicePolicy.json
aws iot list-targets-for-policy --policy-name DevicePolicy --region $REGION     # certificates / groups using it
aws iot list-topic-rules --region $REGION
aws iot get-topic-rule --rule-name ingest_dt --region $REGION > rules/ingest_dt.json
aws iot-data list-named-shadows-for-thing --thing-name pump-001 --endpoint-url https://<prefix>-ats.iot.$REGION.amazonaws.com
aws iot-data get-thing-shadow --thing-name pump-001 --endpoint-url https://<prefix>-ats.iot.$REGION.amazonaws.com shadows/pump-001.json
aws iot get-v2-logging-options --region $REGION
aws iot list-job-executions-for-thing --thing-name pump-001 --region $REGION   # is Jobs in use?
```

CloudWatch metrics (namespace `AWS/IoT`, at least 30 days, 1-hour period): `Connect.Success`, `Connect.AuthError`,
`PublishIn.Success`, `PublishOut.Success`, `Subscribe.Success`, `RuleMessageThrottled`, `TopicMatch` per rule.
Use `aws cloudwatch get-metric-statistics --namespace AWS/IoT --metric-name PublishIn.Success --statistics Sum
--period 3600 ...`. Concurrent connections are not a direct metric: estimate from lifecycle events
(`$aws/events/presence/#` via a rule or Fleet Indexing connectivity) or from device telemetry cadence.

## Pitfalls specific to AWS IoT Core

- **Root CA pinning.** Firmware that ships only Amazon Root CA 1 cannot validate ConnectMQ. Add the new root CA in an
  OTA update **before** the cutover update, or ship both roots.
- **Certificate private keys in secure elements.** Credentials (username/password) must be stored somewhere: plan
  the secure storage on the device (NVS encryption, secure element data slot).
- **ALPN on 443.** Devices that connect on 443 with ALPN for firewall reasons need port 8883 or `wss` on 8084.
- **QoS 1 everywhere.** AWS has no QoS 2, so existing code is QoS 0/1; nothing to change, but do not "upgrade" to
  QoS 2 during the migration.
- **Shadow semantics.** The AWS shadow service merges partial updates, keeps versions and generates delta messages.
  Retained topics store the whole last message: devices must publish full state, and backends must publish full
  desired state.
- **Rules as hidden consumers.** Every topic rule is a consumer. List them all (`list-topic-rules`) and give each a
  replacement owner before moving devices, or data stops flowing silently.
- **Thing-name characters.** Thing names may contain `:` which is fine in MQTT client IDs but check your device
  SDKs; `client_id_map.py` flags unusual characters.
- **Message size.** AWS caps payloads at 128 KB, so payloads already fit ConnectMQ's 256 KB.
- **Session expiry.** Devices configured for multi-day persistent sessions on AWS keep queued messages for at most
  the ConnectMQ plan maximum. Use retained desired-state topics for anything a sleeping device must get on wake-up.
