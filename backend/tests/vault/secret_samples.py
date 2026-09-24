"""Fake credentials for the secret-guard tests, assembled at runtime.

They are built from pieces so that this repository's own source never holds a string that looks
like a real key: GitHub push protection and secret scanners would rightly flag one.
"""

from __future__ import annotations

ANTHROPIC_KEY = "sk-" + "ant-" + "api03-" + "Xy7Kq2Lm9Np4Rs8Tv1Wz" * 2
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GITHUB_PAT = "github" + "_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"
AWS_KEY_ID = "AK" + "IA" + "IOSFODNN7EXAMPLE"

SAMPLES = {
    "anthropic-api-key": ANTHROPIC_KEY,
    "github-token": GITHUB_TOKEN,
    "github-fine-grained-token": GITHUB_PAT,
    "aws-access-key-id": AWS_KEY_ID,
}
