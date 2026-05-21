#!/usr/bin/env python3
"""Thin CLI entrypoint for deterministic /logdrive automation."""

from __future__ import annotations

from logdrive_automation import main


if __name__ == "__main__":
  raise SystemExit(main())
