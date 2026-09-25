#!/usr/bin/env python3
"""
moodle-mailproxy — step 4c: routing + relay

Listens on 127.0.0.1:10025. For each incoming message:
  1. Parse the recipient list, group by destination upstream per the routing table.
  2. For each group, execute the action (archive to disk, or relay via SMTP).
  3. Return a single SMTP response reflecting the combined outcome.

Config: /etc/moodle-mailproxy/config.yaml
Archive: /var/log/moodle-mailproxy/archive/YYYY/MM/DD/<ts>-<hash>.eml
"""

import asyncio
import hashlib
import logging
import os
import signal
import smtplib
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path

import yaml
from aiosmtpd.controller import Controller

CONFIG_PATH = Path("/etc/moodle-mailproxy/config.yaml")
ARCHIVE_ROOT = Path("/var/log/moodle-mailproxy/archive")

log = logging.getLogger("moodle-mailproxy")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, raw: dict):
        self.listen_host: str = raw["listen"]["host"]
        self.listen_port: int = int(raw["listen"]["port"])
        self.listen_hostname: str = raw["listen"]["hostname"]
        self.upstreams: dict = raw["upstreams"]
        self.routes: list = raw["routes"]

        # Validate every route's upstream exists.
        for r in self.routes:
            if r["upstream"] not in self.upstreams:
                raise ValueError(
                    f"route domain={r['domain']} references unknown upstream "
                    f"{r['upstream']!r}"
                )
        # Validate exactly one catch-all and it's last.
        catchalls = [i for i, r in enumerate(self.routes) if r["domain"] == "*"]
        if len(catchalls) != 1 or catchalls[0] != len(self.routes) - 1:
            raise ValueError(
                "routes must contain exactly one '*' catch-all entry, "
                "and it must be the last entry"
            )

    def upstream_for(self, address: str) -> tuple[str, dict]:
        """
        Return (upstream_name, upstream_config) for a given recipient address.
        Domain match is case-insensitive and exact (or '*').
        """
        domain = address.rsplit("@", 1)[-1].lower()
        for r in self.routes:
            if r["domain"] == "*" or r["domain"].lower() == domain:
                name = r["upstream"]
                return name, self.upstreams[name]
        # Unreachable given the catch-all validation above.
        raise RuntimeError(f"no route matched for {address!r}")


def load_config() -> Config:
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    return Config(raw)


# ---------------------------------------------------------------------------
# Upstream actions
# ---------------------------------------------------------------------------

class DeliveryResult:
    """Outcome of attempting one upstream action for a recipient group."""
    OK = "ok"
    TEMP_FAIL = "temp_fail"   # 4xx: retry later
    PERM_FAIL = "perm_fail"   # 5xx: give up

    def __init__(self, status: str, detail: str):
        self.status = status
        self.detail = detail

    def __repr__(self):
        return f"DeliveryResult({self.status}, {self.detail!r})"


def action_archive(envelope, recipients: list, _upstream_cfg: dict) -> DeliveryResult:
    """
    Write the raw message to the archive tree. Recipients list is informational
    (logged) — the archived .eml is the original full message regardless of
    which subset of recipients routed here.
    """
    now = datetime.now(timezone.utc)
    day_dir = ARCHIVE_ROOT / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        content = envelope.content or b""
        digest = hashlib.sha256(content).hexdigest()[:8]
        ts_ns = time.time_ns()
        path = day_dir / f"{ts_ns}-{digest}.eml"

        tmp = path.with_suffix(".eml.tmp")
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(path)
        os.chmod(path, 0o640)
    except Exception as e:
        log.exception("archive write failed")
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"archive write error: {e}")

    return DeliveryResult(DeliveryResult.OK, f"archived to {path} for {recipients}")


def action_smtp_relay(envelope, recipients: list, upstream_cfg: dict) -> DeliveryResult:
    """
    Relay the message to an upstream SMTP server for the given recipient subset.
    The MAIL FROM is preserved from the original envelope.
    """
    host = upstream_cfg["host"]
    port = int(upstream_cfg["port"])
    security = upstream_cfg.get("security", "starttls")
    auth = upstream_cfg.get("auth", "none")
    username = upstream_cfg.get("username")
    password = upstream_cfg.get("password")
    timeout = float(upstream_cfg.get("timeout", 30))

    try:
        if security == "tls":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                      context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)

        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if auth in ("login", "plain"):
                client.login(username, password)
            # sendmail() returns a dict of { recipient: (code, msg) } for
            # any recipients the server refused; empty dict means all accepted.
            refused = client.sendmail(envelope.mail_from, recipients, envelope.content)

        if refused:
            # Some recipients rejected. Treat as permanent fail for this group;
            # we won't retry rejections that the server explicitly refused.
            return DeliveryResult(
                DeliveryResult.PERM_FAIL,
                f"upstream refused recipients: {refused}",
            )
        return DeliveryResult(
            DeliveryResult.OK,
            f"relayed via {host}:{port} to {recipients}",
        )

    except smtplib.SMTPAuthenticationError as e:
        # Auth failure is a config problem on our side, not a transient one.
        # But returning 5xx to Moodle would lose the message — better to
        # return 4xx and have an admin fix the credentials.
        log.error("upstream auth failed for %s: %s", host, e)
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"upstream auth failed: {e}")

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
            ConnectionError, TimeoutError, OSError) as e:
        # Network / transient upstream issue.
        log.warning("upstream connection issue for %s: %s", host, e)
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"upstream connect error: {e}")

    except smtplib.SMTPResponseException as e:
        # The server responded with an SMTP error code.
        # 4xx -> temp, 5xx -> perm.
        status = (DeliveryResult.TEMP_FAIL if 400 <= e.smtp_code < 500
                  else DeliveryResult.PERM_FAIL)
        return DeliveryResult(status, f"upstream {e.smtp_code}: {e.smtp_error!r}")

    except Exception as e:
        log.exception("unexpected upstream error")
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"unexpected error: {e}")


ACTIONS = {
    "archive": action_archive,
    "smtp": action_smtp_relay,
}


# ---------------------------------------------------------------------------
# Combining results
# ---------------------------------------------------------------------------

def combine_results(results: list) -> str:
    """
    Combine per-group DeliveryResult objects into a single SMTP response string.

    Strategy:
      - All OK              -> 250
      - Any TEMP_FAIL       -> 451 (Moodle will retry the whole message)
      - Else (only PERM)    -> 550 (Moodle gives up; nothing would change on retry)
    """
    if all(r.status == DeliveryResult.OK for r in results):
        return "250 Message accepted for processing"
    if any(r.status == DeliveryResult.TEMP_FAIL for r in results):
        return "451 4.3.0 Temporary delivery failure for one or more recipients"
    return "550 5.0.0 Permanent delivery failure for all recipient groups"


# ---------------------------------------------------------------------------
# SMTP handler
# ---------------------------------------------------------------------------

class RoutingHandler:
    def __init__(self, config: Config):
        self.config = config

    async def handle_DATA(self, server, session, envelope):
        size = len(envelope.content) if envelope.content else 0

        # Group recipients by upstream. Preserve original case for delivery,
        # but use lowercased domain as the grouping key.
        groups = defaultdict(list)
        for rcpt in envelope.rcpt_tos:
            upstream_name, _ = self.config.upstream_for(rcpt)
            groups[upstream_name].append(rcpt)

        log.info(
            "received: from=%s rcpts=%s size=%d groups=%s",
            envelope.mail_from,
            list(envelope.rcpt_tos),
            size,
            {k: v for k, v in groups.items()},
        )

        # Execute each group's action. SMTP relay calls block on network I/O;
        # run them in a thread so we don't stall the asyncio event loop.
        loop = asyncio.get_running_loop()
        results = []
        for upstream_name, rcpts in groups.items():
            upstream_cfg = self.config.upstreams[upstream_name]
            action = ACTIONS[upstream_cfg["type"]]
            result = await loop.run_in_executor(
                None, action, envelope, rcpts, upstream_cfg
            )
            log.info("  -> %s: %s (%s)", upstream_name, result.status, result.detail)
            results.append(result)

        response = combine_results(results)
        log.info("response: %s", response)
        return response


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("mail.log").setLevel(logging.WARNING)

    config = load_config()
    log.info("loaded config: upstreams=%s routes=%s",
             list(config.upstreams.keys()),
             [(r["domain"], r["upstream"]) for r in config.routes])

    controller = Controller(
        handler=RoutingHandler(config),
        hostname=config.listen_host,
        port=config.listen_port,
        server_hostname=config.listen_hostname,
    )
    controller.start()
    log.info("listening on %s:%d", config.listen_host, config.listen_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
