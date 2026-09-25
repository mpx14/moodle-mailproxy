#!/usr/bin/env python3
"""
moodle-mailproxy — per-recipient SMTP routing: archive or relay

Listens on 127.0.0.1:10025. For each incoming message:
  1. Parse the recipient list, group by destination upstream per the routing table.
  2. For each group, execute the action (archive to disk, or relay via SMTP).
  3. Return a single SMTP response reflecting the combined outcome.

Config: /etc/moodle-mailproxy/config.yaml (override with --config PATH;
        validate without starting with --check-config)
Archive: /var/log/moodle-mailproxy/archive/YYYY/MM/DD/<ts>-<hash>.eml
"""

import argparse
import asyncio
import hashlib
import ipaddress
import logging
import os
import signal
import smtplib
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from aiosmtpd.controller import Controller

DEFAULT_CONFIG_PATH = Path("/etc/moodle-mailproxy/config.yaml")
ARCHIVE_ROOT = Path("/var/log/moodle-mailproxy/archive")

log = logging.getLogger("moodle-mailproxy")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """The configuration is invalid. Carries every problem found, not just the first."""

    def __init__(self, problems: list):
        self.problems = problems
        super().__init__("; ".join(problems))


TOP_KEYS = {"listen", "upstreams", "routes"}
LISTEN_REQUIRED = {"host", "port", "hostname"}
LISTEN_OPTIONAL = {"allow_non_loopback"}
UPSTREAM_KEYS = {
    "archive": ({"type"}, set()),
    "smtp": ({"type", "host", "port"},
             {"security", "auth", "username", "password", "timeout"}),
}
SECURITY_VALUES = ("starttls", "tls", "none")
# "login" and "plain" are equivalent: either turns authentication on and
# smtplib negotiates the mechanism with the server.
AUTH_VALUES = ("none", "login", "plain")
ROUTE_KEYS = {"domain", "upstream"}


def _check_keys(where: str, d: dict, required: set, optional: set, problems: list):
    for k in sorted(required - d.keys()):
        problems.append(f"{where}: missing required key {k!r}")
    for k in sorted(d.keys() - required - optional, key=str):
        problems.append(f"{where}: unknown key {k!r}")


def _is_port(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname other than "localhost": cannot tell, treat as not loopback


def validate(raw) -> list:
    """Return a list of problems with a parsed config (empty list: valid)."""
    problems = []
    if not isinstance(raw, dict):
        return ["config: top level must be a mapping"]
    _check_keys("config", raw, TOP_KEYS, set(), problems)

    listen = raw.get("listen")
    if isinstance(listen, dict):
        _check_keys("listen", listen, LISTEN_REQUIRED, LISTEN_OPTIONAL, problems)
        if "port" in listen and not _is_port(listen["port"]):
            problems.append("listen.port: must be an integer 1-65535")
        for k in ("host", "hostname"):
            if k in listen and not (isinstance(listen[k], str) and listen[k]):
                problems.append(f"listen.{k}: must be a non-empty string")
        allow = listen.get("allow_non_loopback", False)
        if not isinstance(allow, bool):
            problems.append("listen.allow_non_loopback: must be true or false")
        elif isinstance(listen.get("host"), str) and not allow and not _is_loopback(listen["host"]):
            problems.append(
                f"listen.host: {listen['host']!r} is not a loopback address; the "
                "listener has no authentication (set allow_non_loopback: true to override)")
    elif "listen" in raw:
        problems.append("listen: must be a mapping")

    upstreams = raw.get("upstreams")
    if isinstance(upstreams, dict) and upstreams:
        for name, u in upstreams.items():
            where = f"upstreams.{name}"
            if not isinstance(u, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            t = u.get("type")
            if t not in UPSTREAM_KEYS:
                problems.append(f"{where}.type: must be one of {sorted(UPSTREAM_KEYS)}, got {t!r}")
                continue
            required, optional = UPSTREAM_KEYS[t]
            _check_keys(where, u, required, optional, problems)
            if t != "smtp":
                continue
            if "host" in u and not (isinstance(u["host"], str) and u["host"]):
                problems.append(f"{where}.host: must be a non-empty string")
            if "port" in u and not _is_port(u["port"]):
                problems.append(f"{where}.port: must be an integer 1-65535")
            if u.get("security", "starttls") not in SECURITY_VALUES:
                problems.append(f"{where}.security: must be one of {list(SECURITY_VALUES)}, "
                                f"got {u['security']!r}")
            auth = u.get("auth", "none")
            if auth not in AUTH_VALUES:
                problems.append(f"{where}.auth: must be one of {list(AUTH_VALUES)}, got {auth!r}")
            elif auth != "none":
                for k in ("username", "password"):
                    if not (isinstance(u.get(k), str) and u[k]):
                        problems.append(f"{where}.{k}: required when auth is {auth!r}")
            if "timeout" in u:
                tv = u["timeout"]
                if isinstance(tv, bool) or not isinstance(tv, (int, float)) or tv <= 0:
                    problems.append(f"{where}.timeout: must be a positive number")
    elif "upstreams" in raw:
        problems.append("upstreams: must be a non-empty mapping")

    routes = raw.get("routes")
    if isinstance(routes, list) and routes:
        seen = set()
        for i, r in enumerate(routes):
            where = f"routes[{i}]"
            if not isinstance(r, dict):
                problems.append(f"{where}: must be a mapping")
                continue
            _check_keys(where, r, ROUTE_KEYS, set(), problems)
            d = r.get("domain")
            if not (isinstance(d, str) and d):
                problems.append(f"{where}.domain: must be a non-empty string")
            else:
                if d.lower() in seen:
                    problems.append(f"{where}.domain: {d!r} duplicates an earlier route "
                                    "(it could never match)")
                seen.add(d.lower())
            up = r.get("upstream")
            if isinstance(upstreams, dict) and up not in upstreams:
                problems.append(f"{where}.upstream: unknown upstream {up!r}")
        catchalls = [i for i, r in enumerate(routes)
                     if isinstance(r, dict) and r.get("domain") == "*"]
        if len(catchalls) != 1 or catchalls[0] != len(routes) - 1:
            problems.append("routes: must contain exactly one '*' catch-all entry, "
                            "and it must be the last entry")
    elif "routes" in raw:
        problems.append("routes: must be a non-empty list")

    return problems


class Config:
    def __init__(self, raw: dict):
        problems = validate(raw)
        if problems:
            raise ConfigError(problems)
        self.listen_host: str = raw["listen"]["host"]
        self.listen_port: int = raw["listen"]["port"]
        self.listen_hostname: str = raw["listen"]["hostname"]
        self.upstreams: dict = raw["upstreams"]
        self.routes: list = [
            {"domain": r["domain"].lower(), "upstream": r["upstream"]}
            for r in raw["routes"]
        ]

    def upstream_for(self, address: str) -> tuple[str, dict]:
        """
        Return (upstream_name, upstream_config) for a given recipient address.
        Domain match is case-insensitive and exact (or '*').
        """
        domain = address.rsplit("@", 1)[-1].lower()
        for r in self.routes:
            if r["domain"] == "*" or r["domain"] == domain:
                name = r["upstream"]
                return name, self.upstreams[name]
        # Unreachable given the catch-all validation above.
        raise RuntimeError(f"no route matched for {address!r}")


def load_config(path: Path) -> Config:
    with open(path) as f:
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


def _status_for_code(code: int) -> str:
    return DeliveryResult.TEMP_FAIL if 400 <= code < 500 else DeliveryResult.PERM_FAIL


def _status_for_refused(refused: dict) -> str:
    """Any 4xx refusal makes the group temporary, otherwise permanent."""
    codes = [code for code, _ in refused.values()]
    return (DeliveryResult.TEMP_FAIL if any(400 <= c < 500 for c in codes)
            else DeliveryResult.PERM_FAIL)


def action_smtp_relay(envelope, recipients: list, upstream_cfg: dict) -> DeliveryResult:
    """
    Relay the message to an upstream SMTP server for the given recipient subset.
    The MAIL FROM is preserved from the original envelope.
    """
    host = upstream_cfg["host"]
    port = int(upstream_cfg["port"])
    security = upstream_cfg.get("security", "starttls")  # validated: starttls|tls|none
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
            return DeliveryResult(
                _status_for_refused(refused),
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

    except smtplib.SMTPRecipientsRefused as e:
        # Every recipient refused. Not a subclass of SMTPResponseException.
        return DeliveryResult(_status_for_refused(e.recipients),
                              f"upstream refused all recipients: {e.recipients}")

    except smtplib.SMTPSenderRefused as e:
        # MAIL FROM rejected: usually our sender is not authorised at the
        # provider -- a config problem, treated like an auth failure.
        log.error("upstream refused sender %s: %s %r", e.sender, e.smtp_code, e.smtp_error)
        return DeliveryResult(DeliveryResult.TEMP_FAIL,
                              f"upstream refused sender {e.smtp_code}: {e.smtp_error!r}")

    except smtplib.SMTPConnectError as e:
        log.warning("upstream refused connection %s: %s", host, e)
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"upstream connect refused: {e}")

    except smtplib.SMTPResponseException as e:
        # Must come before OSError: smtplib.SMTPException subclasses OSError.
        return DeliveryResult(_status_for_code(e.smtp_code),
                              f"upstream {e.smtp_code}: {e.smtp_error!r}")

    except OSError as e:
        # Network, TLS and remaining smtplib errors (disconnect, STARTTLS
        # or AUTH not offered).
        log.warning("upstream connection issue for %s: %s", host, e)
        return DeliveryResult(DeliveryResult.TEMP_FAIL, f"upstream connect error: {e}")

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

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Moodle outbound mail proxy")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                    help=f"config file (default: {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--check-config", action="store_true",
                    help="validate the config and exit (0 = valid, 2 = invalid)")
    return ap.parse_args(argv)


# Exit status for an invalid or unreadable config. The unit sets
# RestartPreventExitStatus=2 so systemd does not restart-loop on it.
EXIT_CONFIG = 2


def check_config(path: Path) -> int:
    try:
        config = load_config(path)
    except ConfigError as e:
        print(f"{path}: invalid config:", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_CONFIG
    except (OSError, yaml.YAMLError) as e:
        print(f"{path}: cannot load config: {e}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"{path}: config OK: upstreams={list(config.upstreams)} "
          f"routes={[(r['domain'], r['upstream']) for r in config.routes]}")
    return 0


async def main(config_path: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("mail.log").setLevel(logging.WARNING)

    try:
        config = load_config(config_path)
    except ConfigError as e:
        for p in e.problems:
            log.error("config %s: %s", config_path, p)
        return EXIT_CONFIG
    except (OSError, yaml.YAMLError) as e:
        log.error("cannot load config %s: %s", config_path, e)
        return EXIT_CONFIG
    log.info("loaded config %s: upstreams=%s routes=%s", config_path,
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
    return 0


if __name__ == "__main__":
    args = parse_args()
    if args.check_config:
        sys.exit(check_config(args.config))
    sys.exit(asyncio.run(main(args.config)))
