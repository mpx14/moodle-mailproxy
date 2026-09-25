# moodle-mailproxy

A small SMTP proxy that sits between Moodle and an upstream mail relay and
routes every recipient by domain: either relay the message through an
authenticated SMTP provider, or write it to a local archive instead of
sending it.

It was built for a production Moodle 4.5 site where some accounts have
synthetic addresses (users imported without a real mailbox). Mail to those
domains is kept on disk for inspection instead of being handed to a provider
that cannot deliver it.

## Status

- `deployed-*` tags mark the code running on the reference production
  host, with the SHA-256 hashes of the installed files in the tag message
  (for `deployed-2026-04-15`, in the tagged commit's message). The current
  one is `deployed-2026-09-25`; `deployed-2026-04-15` is the original code
  as found on the host. Commits after the latest `deployed-*` tag are not
  deployed.
- **Tested in production:** Debian 12, Python 3.11.2, python3-aiosmtpd 1.4.3,
  python3-yaml 6.0, Moodle 4.5, one `archive` upstream plus one `smtp`
  upstream using STARTTLS and LOGIN on port 587.
- **Not tested:** other distributions or Python versions (the code needs
  Python 3.9 or later for its type annotations), `security: tls`, more than
  one `smtp` upstream, and a fresh install following the steps below. Those
  steps are reconstructed from the production host's state, not replayed on
  a clean machine.

## How it works

```
Moodle ──SMTP, 127.0.0.1:10025, no TLS, no auth──▶ moodle-mailproxy
                                                      │
                          per recipient, by domain    ├──▶ archive: .eml on local disk
                                                      └──▶ smtp: STARTTLS/TLS + AUTH relay
```

For each message:

1. Recipients are grouped by the first route whose domain matches
   (exact, case-insensitive; `*` is the catch-all and must be last).
2. Each group is handed to its upstream: `archive` writes the whole
   message to `/var/log/moodle-mailproxy/archive/YYYY/MM/DD/<ns>-<hash>.eml`;
   `smtp` relays it to that group's recipients only.
3. One SMTP reply goes back to Moodle: `250` if every group succeeded,
   `451` if any group failed temporarily, otherwise `550`.

The envelope sender from Moodle is passed through unchanged, so the relay
provider must be allowed to send for the domain of Moodle's no-reply address
(sender verification, SPF and DKIM are configured at the provider).

## Requirements

```sh
sudo apt install python3 python3-aiosmtpd python3-yaml
```

## Installation

Run from a checkout of this repository.

```sh
# Service account: no home, no login shell
sudo useradd --system --user-group --home-dir /nonexistent --no-create-home \
    --shell /usr/sbin/nologin moodle-mailproxy

# Code: owned by root so the daemon cannot modify itself
sudo install -d -o root -g root -m 0755 /opt/moodle-mailproxy
sudo install -o root -g root -m 0644 src/proxy.py src/prune.py /opt/moodle-mailproxy/

# Config: readable by the service group, writable by root only
sudo install -d -o root -g moodle-mailproxy -m 0750 /etc/moodle-mailproxy
sudo install -o root -g moodle-mailproxy -m 0640 \
    config/config.example.yaml /etc/moodle-mailproxy/config.yaml
sudoedit /etc/moodle-mailproxy/config.yaml

# Archive: the only path the sandboxed services can write to
sudo install -d -o moodle-mailproxy -g moodle-mailproxy -m 0750 /var/log/moodle-mailproxy

# Units
sudo install -o root -g root -m 0644 systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now moodle-mailproxy.service moodle-mailproxy-prune.timer
```

The reference production host differs from this in two ways, both harmless
at runtime because `ProtectSystem=strict` makes them read-only to the
service: `/opt/moodle-mailproxy` and `/etc/moodle-mailproxy` are owned by
the service account there. The ownership above is the recommended one.

### Verify

```sh
journalctl -u moodle-mailproxy -n 20      # expect "loaded config /etc/moodle-mailproxy/config.yaml: ..." and "listening on 127.0.0.1:10025"
ss -ltn | grep 10025                      # must show 127.0.0.1 only
```

Send a test message to a domain routed to `archive` (adjust the address to
your config) and confirm an `.eml` file appears:

```sh
python3 - <<'PY'
import smtplib
from email.message import EmailMessage
m = EmailMessage()
m["From"], m["To"], m["Subject"] = "noreply@example.org", "test@noemail.example.org", "mailproxy test"
m.set_content("test")
with smtplib.SMTP("127.0.0.1", 10025) as s:
    s.send_message(m)
PY
sudo find /var/log/moodle-mailproxy/archive -name '*.eml' -mmin -5
```

## Moodle configuration

*Site administration → Server → Email → Outgoing mail configuration:*

| Setting | Value |
|---|---|
| SMTP hosts (`smtphosts`) | `127.0.0.1:10025` |
| SMTP security (`smtpsecure`) | None |
| SMTP username / password | empty |
| SMTP session limit (`smtpmaxbulk`) | `1` |
| No-reply address (`noreplyaddress`) | an address in a domain your relay may send for |

TLS and authentication happen only on the proxy's upstream connection.

`smtpmaxbulk = 1` is recommended because the proxy answers once per
message: with one message per session, a failure on one message cannot
blur the outcome of others. It is also the only value tested.

## Operations

- **Logs:** `journalctl -u moodle-mailproxy`. Every message produces a
  `received:` line (sender, recipients, size, groups), one line per upstream
  with its outcome, and the final `response:`.
- **No `received:` line means Moodle never handed the message over.** For
  example, a forum post to a forum with no subscribers is processed by
  Moodle and marked as mailed without sending anything. Check the Moodle
  side first in that case.
- **Config changes:** validate first, then restart:

  ```sh
  sudo -u moodle-mailproxy python3 /opt/moodle-mailproxy/proxy.py --check-config
  sudo systemctl restart moodle-mailproxy.service
  ```

  `--check-config` prints every problem it finds and exits `2` if the config
  is invalid, without touching the running service. The same checks run at
  startup: unknown or missing keys, an unknown upstream `type`, `security`
  or `auth` value, `auth` without `username`/`password`, non-integer ports,
  a `listen.host` that is not loopback (unless `allow_non_loopback: true`),
  routes naming an undefined upstream, duplicate route domains, and a
  missing or misplaced `*` route. An invalid config makes the daemon exit
  with status `2`, which the unit does not restart, and the reasons are in
  the journal. `--config PATH` selects another config file.
- **Archive retention:** `moodle-mailproxy-prune.timer` runs hourly and
  deletes archived messages older than 14 days, then the oldest ones until
  the archive is under 500 MB. Both limits are constants in `src/prune.py`.
  Run it by hand with `sudo systemctl start moodle-mailproxy-prune.service`.

## Security

- **The listener has no authentication.** It must stay bound to loopback.
  Bound to any reachable interface, it is an open relay using your
  provider credentials.
- Both units run as the unprivileged service account under a strict systemd
  sandbox: read-only filesystem except `/var/log/moodle-mailproxy`, no
  capabilities, and a restricted syscall set. The pruner also has no
  network access. The daemon's network access is limited to IPv4/IPv6
  sockets, but not to particular destinations, because it must reach the
  relay. Only the listen address in the config keeps the listener local.
- Relay credentials are stored in plain text in the config file. They are
  protected by its `0640 root:moodle-mailproxy` permissions only.

## Privacy

This matters when the users are pupils and parents.

- The journal records the sender, all recipient addresses and the size of
  every message. How long that is kept depends on the host's journald
  settings.
- The archive stores complete messages (headers, bodies, attachments) for
  up to 14 days or 500 MB, readable by root and the service group.

## How Moodle handles a failed send

Read in the Moodle 4.5.13 source, not observed: the reference deployment
has no failed send in its logs so far.

- Moodle does not distinguish `451` from `550`. PHPMailer runs without
  exceptions and reports any SMTP error as a failed send; `email_to_user()`
  then logs a `\core\event\email_failed` event and returns false.
- Whether the message is retried depends on the caller, not on the reply:
  - Forum post notifications and digests run as adhoc tasks and are
    retried: 12 attempts, the delay starting at one minute and doubling up
    to 24 hours, about 34 hours in total. Only the failed posts of a
    notification task are re-queued.
  - Emails for group-conversation messages stay queued until the next run
    of the daily email task.
  - A caller that sends outside such a task and only checks the return
    value gets no retry: the message is lost. These callers have not been
    enumerated.
- Each retry of a forum notification calls `message_send()` again, which
  should also create another in-app notification for the recipient.
- `email_to_user()` addresses exactly one recipient per message. The only
  exception adds the support user when an attachment path is rejected as
  unsafe.

To check a Moodle site for failed sends, look for `email_failed` events in
the standard log, and for `mod_forum` adhoc tasks with a non-zero
`faildelay`.

## Known limitations of the current code

Found by code review, not by incidents in production:

1. If one recipient group succeeds and another fails temporarily, the whole
   message gets `451`. If the client retries, the successful group receives
   a duplicate. With Moodle this needs two recipients in different routes,
   which `email_to_user()` produces only in the unsafe-attachment case.
2. A temporary failure is not a delayed delivery. The code answers `451`
   (on upstream authentication failure, a refused sender, or a temporary
   upstream error) so that a retrying client can resend later, but Moodle
   retries only when the send runs inside a retrying task (forum mail,
   group-conversation emails). Otherwise treat a failed send as lost. See
   [How Moodle handles a failed send](#how-moodle-handles-a-failed-send).
3. A partial recipient refusal by the relay fails the whole group, but the
   accepted recipients in that group have already been sent the message.
4. An `.eml.tmp` left behind by a crash during an archive write is never
   pruned.
5. The archive path and the retention limits are hardcoded. The config
   path defaults to `/etc/moodle-mailproxy/config.yaml` and can be changed
   with `--config`.

## Licence

BSD 2-Clause. See [LICENSE](LICENSE).
