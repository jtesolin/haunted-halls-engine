"""Lightweight semantic-memory helpers.

This module provides a small, dependency-free approximation of vector search:

- tokenize(): normalize free text into lowercase word tokens
- build_embedding(): map token counts into a fixed-size signed hash vector
- serialize/deserialize_embedding(): persist vectors in SQLite as JSON
- cosine_similarity(): rank query vectors against stored memory vectors

Design intent:
- Stable across restarts (uses SHA-256 hashing, not Python's randomized hash)
- Fast and local (no external embedding API calls)
- Good enough for relevance filtering of recent campaign memories

Limitations:
- This is not model-quality semantic embedding
- Lexical overlap and hash collisions can affect ranking quality
- For higher recall/precision, replace with model embeddings + pgvector
"""

import hashlib
import json
import math
import re
from collections import Counter

EMBEDDING_DIMENSIONS = 64
TOKEN_PATTERN = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.lower())


def build_embedding(text: str) -> list[float]:
    tokens = tokenize(text)
    if not tokens:
        return [0.0] * EMBEDDING_DIMENSIONS

    counts = Counter(tokens)
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token, count in counts.items():
        token_digest = hashlib.sha256(token.encode("utf-8")).digest()
        sign_digest = hashlib.sha256(f"{token}:sign".encode("utf-8")).digest()
        bucket = token_digest[0] % EMBEDDING_DIMENSIONS
        sign = -1.0 if sign_digest[0] % 2 else 1.0
        vector[bucket] += sign * float(count)

    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def serialize_embedding(embedding: list[float]) -> str:
    return json.dumps(embedding)


def deserialize_embedding(embedding_json: str | None) -> list[float]:
    if not embedding_json:
        return [0.0] * EMBEDDING_DIMENSIONS
    try:
        data = json.loads(embedding_json)
    except json.JSONDecodeError:
        return [0.0] * EMBEDDING_DIMENSIONS
    if not isinstance(data, list):
        return [0.0] * EMBEDDING_DIMENSIONS
    embedding: list[float] = []
    for value in data[:EMBEDDING_DIMENSIONS]:
        try:
            embedding.append(float(value))
        except (TypeError, ValueError):
            embedding.append(0.0)
    if len(embedding) < EMBEDDING_DIMENSIONS:
        embedding.extend([0.0] * (EMBEDDING_DIMENSIONS - len(embedding)))
    return embedding


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    dot_product = sum(left[index] * right[index] for index in range(length))
    left_magnitude = math.sqrt(sum(value * value for value in left[:length]))
    right_magnitude = math.sqrt(sum(value * value for value in right[:length]))
    if left_magnitude == 0 or right_magnitude == 0:
        return 0.0
    return dot_product / (left_magnitude * right_magnitude)
