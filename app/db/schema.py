from __future__ import annotations

from sqlalchemy import Boolean as BooleanType
from sqlalchemy import Column, Float as FloatType, ForeignKey, Integer as IntegerType
from sqlalchemy import MetaData, String as StringType, Table, Text as TextType
from sqlalchemy import UniqueConstraint


metadata = MetaData()


def Integer(name: str, **kwargs):
    return Column(name, IntegerType, **kwargs)


def String(name: str, *args, **kwargs):
    return Column(name, StringType, *args, **kwargs)


def Text(name: str, **kwargs):
    return Column(name, TextType, **kwargs)


def Boolean(name: str, **kwargs):
    return Column(name, BooleanType, **kwargs)


def Float(name: str, **kwargs):
    return Column(name, FloatType, **kwargs)

internal_users = Table(
    "internal_users",
    metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("user_id", nullable=False, unique=True),
    String("identity_provider", nullable=False),
    String("provider_issuer", nullable=False),
    String("provider_subject", nullable=False),
    String("email", nullable=False),
    Boolean("email_verified", nullable=False),
    String("display_name"),
    String("avatar_url"),
    Text("created_at", nullable=False),
    Text("updated_at", nullable=False),
    Text("last_login_at", nullable=False),
    UniqueConstraint("provider_issuer", "provider_subject", name="uq_internal_users_provider_identity"),
)

campaigns = Table(
    "campaigns", metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("campaign_id", nullable=False, unique=True),
    String("owner_user_id", ForeignKey("internal_users.user_id", ondelete="RESTRICT")),
    String("name", nullable=False), Text("description"), Text("state"),
    Text("created_at", nullable=False),
)

characters = Table(
    "characters", metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("character_id", nullable=False, unique=True),
    String("campaign_id", ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False),
    String("name", nullable=False), Text("description"), Text("created_at", nullable=False),
)

turns = Table(
    "turns", metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("turn_id", nullable=False, unique=True),
    String("campaign_id", ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False),
    String("role", nullable=False), Text("content", nullable=False), Text("created_at", nullable=False),
)

model_requests = Table(
    "model_requests", metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("request_id", nullable=False, unique=True),
    String("owner_user_id", ForeignKey("internal_users.user_id", ondelete="RESTRICT")),
    String("campaign_id", nullable=False), String("turn_id", nullable=False),
    String("agent_name", nullable=False), String("model", nullable=False),
    Integer("estimated_input_tokens", nullable=False),
    Integer("estimated_output_tokens", nullable=False),
    Integer("actual_input_tokens", nullable=True),
    Integer("cached_input_tokens", nullable=True),
    Integer("cache_write_input_tokens", nullable=True),
    Integer("actual_output_tokens", nullable=True),
    Integer("reasoning_output_tokens", nullable=True),
    Integer("actual_total_tokens", nullable=True),
    Integer("latency_ms", nullable=False),
    Boolean("success", nullable=False), Text("failure_reason"), Float("cost_estimate"),
    Text("created_at", nullable=False),
)

game_events = Table(
    "game_events", metadata,
    Integer("id", primary_key=True, autoincrement=True), String("event_id", nullable=False, unique=True),
    String("campaign_id", ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False),
    String("turn_id"), String("type", nullable=False), Text("payload_json"), Text("created_at", nullable=False),
)

summaries = Table(
    "summaries", metadata,
    Integer("id", primary_key=True, autoincrement=True),
    String("campaign_id", ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False),
    Text("summary", nullable=False), Text("created_at", nullable=False),
)

memories = Table(
    "memories", metadata,
    Integer("id", primary_key=True, autoincrement=True), String("memory_id", nullable=False, unique=True),
    String("campaign_id", ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False),
    String("kind", nullable=False), Text("content", nullable=False), Text("embedding_json", nullable=False),
    Float("importance", nullable=False), String("source_event_id"), Text("created_at", nullable=False),
)