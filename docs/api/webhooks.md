# Webhook delivery

Covenant Radar sends webhook notifications as signed `POST` requests. The
sender delivers only the already-authorised notification payload: references,
event metadata, and derived values. Personal-class fields are rejected before
the request is made.

## Request format

Each request has `Content-Type: application/json` and these headers:

| Header | Meaning |
|---|---|
| `X-Covenant-Radar-Timestamp` | Unix timestamp in seconds used for signing |
| `X-Covenant-Radar-Signature` | `v1=` followed by the lowercase HMAC-SHA256 digest |
| `X-Covenant-Radar-Event-ID` | Stable event identifier for retry deduplication |
| `X-Covenant-Radar-Event-Type` | Notification template name |
| `Idempotency-Key` | Receivers should use `X-Covenant-Radar-Event-ID` as their idempotency key |

The signed message is the exact UTF-8 request body prefixed with the timestamp
and a period:

```text
<timestamp>.<raw request body>
```

The JSON body is canonicalised with sorted keys, compact separators, UTF-8
characters, and no non-finite numbers. Its shape is:

```json
{
  "data": {
    "borrower_reference": "BOR-118",
    "summary": "Covenant moved to act."
  },
  "event_id": "sha256-hex-value",
  "occurred_at": "2026-08-31T09:00:00+00:00",
  "subject": {
    "id": "opaque-subject-uuid",
    "type": "borrower"
  },
  "type": "band_change",
  "version": 1
}
```

`subject` and `occurred_at` may be `null` when the notification has no
subject or scheduled time. The event ID is stable across delivery retries.
Receivers must acknowledge an event only after persisting or otherwise
atomically processing its event ID.

## Receiver verification

Store the signing secret in the receiver's secret store. Do not put it in a
URL, log line, or source file. Verify the timestamp before accepting the
signature; the default replay window is five minutes.

The following is a complete reference implementation using only the Python
standard library. `body` must be the raw bytes read from the request, before
JSON parsing.

```python
import hashlib
import hmac
import time


def verify_webhook(body: bytes, timestamp: str, signature: str, secret: bytes) -> bool:
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - sent_at) > 300:
        return False
    if len(signature) != 67 or not signature.startswith("v1="):
        return False
    signed = timestamp.encode("ascii") + b"." + body
    expected = "v1=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
```

The receiver should also require HTTPS, reject a second use of an already
processed event ID, and return a 2xx status only after the event is durable.

## Delivery and retry policy

The sender does not follow redirects. A 2xx response marks the notification
sent. Timeouts, connection failures, HTTP 408, 425, 429, and 5xx responses
are transient failures and are retried with exponential backoff. A valid
`Retry-After` header is honoured up to one hour. Other 3xx and 4xx responses
are permanent failures.

The notification row is the durable delivery record. It stores the attempt
count, scheduled retry time, last error, sent time, and dead-letter time. On
the configured retry limit, the row becomes `dead_lettered` and the configured
alert callback is invoked. An alerting failure cannot roll back the durable
dead-letter transition.

An administrator can list webhook dead letters with
`NotificationService.list_dead_letters()` and replay one with
`replay_dead_letter(notification_id)`. Replay resets only the active retry
cycle; it does not create a second notification. A row already marked `sent`
is an idempotent no-op, so a race between acknowledgement and an operator's
replay action cannot duplicate delivery.

## Endpoint configuration

Endpoints are resolved by recipient ID through `WebhookEndpointRegistry` or a
deployment-owned resolver. Only HTTP(S) URLs without credentials, query
strings, fragments, or control characters are accepted. TLS certificate
verification remains enabled and redirects remain disabled. Removing an
endpoint while notifications are queued dead-letters those notifications with
an explicit removal reason.
