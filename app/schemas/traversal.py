"""Finite authored obstacles, not executable model-authored rules."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RouteKind(StrEnum):
    VERTICAL = "vertical"
    SURFACE = "surface"
    GAP = "gap"
    WATER = "water"


class TraversalRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    kind: RouteKind
    # Authored crossing length (vertical rise/drop for vertical routes), in metres.
    distance_metres: int = Field(strict=True, ge=1, le=12)
    valid_takeoff: bool = Field(strict=True)
    valid_landing: bool = Field(strict=True)
    clear_vertical_path: bool = Field(default=False, strict=True)
    continuous_support: bool = Field(default=False, strict=True)
    water_surface: bool = Field(default=False, strict=True)

    @field_validator("name", "description", "origin", "destination")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Route text and endpoints must not be blank.")
        return value

    @model_validator(mode="after")
    def coherent_constraints(self) -> "TraversalRoute":
        if self.origin == self.destination:
            raise ValueError("A traversal route must cross between distinct rooms.")
        if self.clear_vertical_path and self.kind != RouteKind.VERTICAL:
            raise ValueError("Only vertical routes have a clear vertical path.")
        if self.water_surface and self.kind != RouteKind.WATER:
            raise ValueError("Only water routes have a water surface.")
        if self.continuous_support and self.kind not in {RouteKind.VERTICAL, RouteKind.SURFACE}:
            raise ValueError("Continuous climb support must be vertical or surface traversal.")
        return self


class LocalTraversalRoute(BaseModel):
    """Bounded discoverable context; no remote graph or eligibility adjudication."""

    route_id: str
    name: str
    description: str
    destination: str
    destination_name: str
    kind: RouteKind
    distance_metres: int
    valid_takeoff: bool
    valid_landing: bool
    clear_vertical_path: bool
    continuous_support: bool
    water_surface: bool
