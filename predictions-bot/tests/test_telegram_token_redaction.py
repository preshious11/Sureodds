"""Telegram bot tokens must never reach an error trace, a log line or a terminal.

All tokens below are FAKE and shaped like real ones only so the redaction patterns are exercised."""
import logging
import sys

import pytest
import requests

from predbot.util.redact import RedactingFilter, install_excepthook, redact, safe_error

FAKE_TOKEN = "123456789:AAFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEXY"
FAKE_SECRET_VALUE = "not-a-known-shape-but-configured-secret"


def _connection_error():
    # requests puts the full request URL into the exception text; for Telegram that URL carries the token in its path
    return requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
        f"/bot{FAKE_TOKEN}/getChat (Caused by NewConnectionError)")


def test_redact_masks_the_token_in_a_telegram_url():
    out = redact(f"https://api.telegram.org/bot{FAKE_TOKEN}/getChat")
    assert FAKE_TOKEN not in out
    assert "/bot***" in out


def test_redact_masks_a_configured_secret_of_any_shape():
    out = redact(f"request failed for value {FAKE_SECRET_VALUE}", known_secrets=[FAKE_SECRET_VALUE])
    assert FAKE_SECRET_VALUE not in out


def test_safe_error_keeps_the_class_and_drops_the_url():
    out = safe_error(_connection_error(), known_secrets=[FAKE_TOKEN])
    assert out.startswith("ConnectionError:")
    assert FAKE_TOKEN not in out and "api.telegram.org/bot" not in out.replace("/bot***", "")


def test_logging_filter_masks_an_exception_that_embeds_the_url(caplog):
    logger = logging.getLogger("test.redaction")
    flt = RedactingFilter([FAKE_TOKEN])
    logger.addFilter(flt)
    try:
        try:
            raise _connection_error()
        except requests.exceptions.ConnectionError:
            with caplog.at_level(logging.ERROR, logger="test.redaction"):
                logger.exception("telegram transport failure")
    finally:
        logger.removeFilter(flt)
    text = caplog.text
    assert FAKE_TOKEN not in text
    assert "telegram transport failure" in text


@pytest.fixture
def restore_excepthook():
    saved = sys.excepthook
    yield
    sys.excepthook = saved


def test_uncaught_exception_output_is_redacted(restore_excepthook, capsys):
    install_excepthook([FAKE_TOKEN])
    try:
        raise _connection_error()
    except requests.exceptions.ConnectionError:
        sys.excepthook(*sys.exc_info())
    err = capsys.readouterr().err
    assert "ConnectionError" in err
    assert FAKE_TOKEN not in err


def test_installing_the_hook_twice_only_adds_secrets(restore_excepthook, capsys):
    install_excepthook([FAKE_TOKEN])
    first = sys.excepthook
    install_excepthook([FAKE_SECRET_VALUE])
    assert sys.excepthook is first                       # no stacking of hooks
    try:
        raise RuntimeError(f"value {FAKE_SECRET_VALUE} and {FAKE_TOKEN}")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    err = capsys.readouterr().err
    assert FAKE_SECRET_VALUE not in err and FAKE_TOKEN not in err
