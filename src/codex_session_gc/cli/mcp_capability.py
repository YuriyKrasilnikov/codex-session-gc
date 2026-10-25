# Copyright (c) 2026

"""Explicit startup capability for MCP observation and cleanup operations."""

from enum import StrEnum


class McpCapability(StrEnum):
    """Choose observation only or the existing confirmed cleanup tools."""

    READ_ONLY = "read_only"
    CLEANUP = "cleanup"
