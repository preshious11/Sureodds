import logging
import logging.handlers
import sys
from typing import Optional


def setup_logging(level: str = "INFO", log_file: Optional[str] = None, secrets=()) -> None:
    """Log to stdout and, when ``log_file`` is given, to a rotating file too, so a
    generation cycle (cache hit/miss, API spend, quota refusals, the funnel) can
    be read back after the fact instead of only scrolling past in a terminal."""
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            handlers.append(logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"))
        except OSError as exc:            # an unwritable log path must never stop the bot
            print(f"could not open log file {log_file!r}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # every record passes through the redactor: keys in URLs, tokens, e-mails and account ids never reach a log
    from predbot.util.redact import install
    install(secrets)
