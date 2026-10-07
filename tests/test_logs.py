"""Tests for the in-memory log buffer (relay/logs.py)."""

import logging
import subprocess
import sys

from relay import logs


def test_buffer_captures_and_tails():
    logging.getLogger().setLevel(logging.INFO)
    buffer = logs.install(logging.INFO)
    logger = logging.getLogger("relay.test")
    logger.info("hello-log-marker")
    assert any("hello-log-marker" in line for line in buffer.tail(10))
    assert buffer.tail(0) == []


def test_tail_defaults_to_empty_when_unused():
    # a fresh buffer with no records yields an empty list
    assert logs.LogBuffer().tail(5) == []


def test_import_in_subprocess_without_main():
    # importing the module must not install handlers as a side effect
    code = "import relay.logs; assert relay.logs.tail(3) == []"
    subprocess.run([sys.executable, "-c", code], check=True)
