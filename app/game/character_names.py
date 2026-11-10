from __future__ import annotations

from faker import Faker

_faker = Faker()


def generate_character_name() -> str:
    """Generate a random display name for a newly created character."""
    return _faker.name()
