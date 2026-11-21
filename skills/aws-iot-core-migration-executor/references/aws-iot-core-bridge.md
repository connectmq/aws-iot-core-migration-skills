# Bridging AWS IoT Core and ConnectMQ During the Parallel Run

AWS IoT Core cannot open an MQTT bridge to another broker, and ConnectMQ does not bridge out. The parallel run
therefore uses a client that connects to both: `relay.py` (recommended) or a local Mosquitto with two bridges.
Either way it is one connection on each side; on ConnectMQ it counts toward the concurrent-connection limit, on AWS
it is billed like any other device (connectivity and messages).

## 1. Create a relay thing with least privilege

```bash
REGION=eu-west-1
aws iot create-thing --thing-name connectmq-relay --region $REGION
aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile relay.cert.pem --private-key-outfile relay.private.key \
  --region $REGION > relay-cert.json
curl -o AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem
aws iot create-policy --policy-name ConnectMQRelayPolicy --policy-document file://relay-policy.json --region $REGION
aws iot attach-policy --policy-name ConnectMQRelayPolicy --target "$(jq -r .certificateArn relay-cert.json)" --region $REGION
aws iot attach-thing-principal --thing-name connectmq-relay --principal "$(jq -r .certificateArn relay-cert.json)" --region $REGION
```

`relay-policy.json` for forward topics `dt/#` (devices on AWS -> ConnectMQ) and reverse topics `cmd/#`
(ConnectMQ backends -> devices still on AWS). Replace Region and account:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "iot:Connect",
      "Resource": "arn:aws:iot:eu-west-1:123456789012:client/connectmq-relay"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Subscribe",
      "Resource": "arn:aws:iot:eu-west-1:123456789012:topicfilter/dt/#"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Receive",
      "Resource": "arn:aws:iot:eu-west-1:123456789012:topic/dt/*"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": "arn:aws:iot:eu-west-1:123456789012:topic/cmd/*"
    }
  ]
}
```

In AWS policies the `topicfilter/` resource is matched against the literal filter the client subscribes with, so
`topicfilter/dt/#` allows exactly the subscription `dt/#`. Add one statement per forward filter if you relay several.
Keep the private key out of source control and on the relay host only.

## 2. ConnectMQ side

- Credential for the relay (or the `_relay` suffix of an existing credential: the relay connects as
  `<ISSUED_ID>_relay`).
- ACL rules before enabling ACL: allow `publish` on the forward topics and `subscribe` on the reverse topics for
  subject clientid `<ISSUED_ID>_relay`.

## 3. Run the relay

```bash
export CONNECTMQ_USERNAME=... CONNECTMQ_PASSWORD=... CONNECTMQ_CLIENT_ID=<ISSUED_ID>
python scripts/relay.py \
  --source mqtts://<prefix>-ats.iot.eu-west-1.amazonaws.com:8883 --source-client-id connectmq-relay \
  --source-cafile AmazonRootCA1.pem --source-cert relay.cert.pem --source-key relay.private.key \
  --target mqtts://BROKER_HOST:8883 \
  --forward 'dt/#' --reverse 'cmd/#'
```

- The source client ID must match the `iot:Connect` resource (`connectmq-relay`).
- AWS IoT Core has no QoS 2: keep `--qos 1` (default).
- MQTT 5 is supported by AWS IoT Core. If the relay's subscribe options (no-local, retain-as-published) are refused
  in your account/Region, run with `--source-protocol 4`; retained flags are then only preserved for the initial
  retained delivery, so copy retained state with `copy_retained.py` / `aws_shadows_to_retained.py` instead.
- Run it under a supervisor (systemd `Restart=always`, ECS/Kubernetes, Docker `restart: always`) close to the AWS
  Region. Stats print every 30 s.

## 4. Shadows and reserved topics during the parallel run

The relay copies messages, it does not transform payloads. Two options for shadow state:

1. **Periodic copy (simplest).** Run `aws_shadows_to_retained.py --fetch` and `copy_retained.py --import` on a
   schedule (e.g. every 5 minutes) while devices are split between the brokers. ConnectMQ consumers read
   `things/<t>/shadow/reported` retained state.
2. **Relay raw shadow traffic.** A second relay instance with a prefix rewrite forwards the AWS shadow topics as
   they are, e.g. `--forward '$aws/things/+/shadow/update/accepted' --strip-prefix '$aws/things/' --add-prefix
   'aws-shadow/'`. Consumers then parse the AWS shadow document format until the last device moves. The relay
   policy needs `topicfilter/$aws/things/+/shadow/update/accepted` and `topic/$aws/things/*/shadow/update/accepted`.

Basic Ingest (`$aws/rules/...`) messages never reach subscribers on AWS; relay the rule's output instead (add a
`republish` action to a plain topic during the parallel run) or keep the rule until the publishers move.

## 5. Alternative: local Mosquitto as a hub

When a site already runs Mosquitto, or the relay must sit on-premises, one Mosquitto can bridge to both brokers:

```conf
# --- AWS IoT Core ---
connection aws
address <prefix>-ats.iot.eu-west-1.amazonaws.com:8883
bridge_protocol_version mqttv311
bridge_cafile /etc/mosquitto/certs/AmazonRootCA1.pem
bridge_certfile /etc/mosquitto/certs/relay.cert.pem
bridge_keyfile /etc/mosquitto/certs/relay.private.key
remote_clientid connectmq-relay
try_private false
notifications false
bridge_attempt_unsubscribe false
cleansession true
topic dt/# in 1
topic cmd/# out 1

# --- ConnectMQ ---
connection connectmq
address BROKER_HOST:8883
bridge_capath /etc/ssl/certs
remote_username CONNECTMQ_USERNAME
remote_password CONNECTMQ_PASSWORD
remote_clientid ISSUED_ID_relay
bridge_protocol_version mqttv311
try_private false
notifications false
cleansession false
topic dt/# out 1
topic cmd/# in 1
```

`dt/#` comes in from AWS and goes out to ConnectMQ; `cmd/#` comes in from ConnectMQ and goes out to AWS. The two
directions never overlap, so nothing loops. The same IoT policy as above applies to `connectmq-relay`.

## 6. Moving devices

- **Two firmware/config steps.** Step 1 (while still on AWS): add the ConnectMQ root CA and storage for
  username/password. Step 2: switch host, port, credentials and client ID (`<ISSUED_ID>_<thing>`), and the
  redesigned topics from `migration/topic-map.csv`.
- **Delivery.** AWS IoT Jobs is a practical way to trigger step 2 per thing group (`aws iot create-job --targets
  <thing-group-arn> ...`). Do not put passwords in job documents in clear text: point the job at a short-lived
  presigned URL or your provisioning API, where each device fetches its own credential.
- **Rollback lever.** Keep each device's AWS certificate ACTIVE until its wave has soaked. Firmware should fall back
  to the previous endpoint configuration when it cannot connect to ConnectMQ for a defined time (e.g. 30 minutes),
  or accept a rollback command over the reverse relay direction.

## 7. Decommission (only after the rollback window)

```bash
aws iot disable-topic-rule --rule-name ingest_dt                    # reversible: enable-topic-rule
aws iot update-certificate --certificate-id <id> --new-status INACTIVE   # reversible until deleted
aws iot detach-policy --policy-name DevicePolicy --target <certificateArn>
# after the agreed retention period:
aws iot delete-certificate --certificate-id <id>
aws iot delete-topic-rule --rule-name ingest_dt
aws iot delete-thing --thing-name <thing>                          # only if the registry is no longer needed
```

Deactivate before deleting, one thing group at a time, and confirm in CloudWatch (`Connect.Success` falls to zero
for the group) before each step. Stop the relay last, then deactivate and delete the relay certificate and policy.
