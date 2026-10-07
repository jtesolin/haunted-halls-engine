"""Shared deterministic validation for one atomic authored crossing."""

from app.game.world import World, normalize_identifier
from app.schemas.generated_abilities import GeneratedAbilityMechanics, TraversalMethod
from app.schemas.traversal import RouteKind, TraversalRoute


def resolve_local_route(
    world: World, origin: str, target: str | None
) -> tuple[TraversalRoute | None, str | None, str | None]:
    reference = normalize_identifier(target or "")
    if not reference:
        return None, "route_target_missing", "This ability needs a specific crossing nearby to carry you over; none was chosen."
    matches = [
        route for route in world.traversal_routes
        if reference and reference in world.route_references(route)
    ]
    local = [route for route in matches if route.origin == origin]
    if len(local) > 1:
        return None, "route_ambiguous", "That reference names multiple nearby crossings; name a route."
    if not local:
        if matches:
            return None, "route_not_local", "That crossing does not start here."
        return None, "route_unknown", "Nothing here offers a crossing to that place."
    return local[0], None, None


def validate_route_capability(
    route: TraversalRoute, mechanics: GeneratedAbilityMechanics
) -> tuple[str | None, str | None]:
    """Metres are authored crossing length, not coordinate physics or free movement."""
    if not route.valid_takeoff or not route.valid_landing:
        return "route_unstable_endpoint", "This crossing lacks a stable takeoff or landing."
    method = mechanics.traversal_method
    compatible = (
        method == TraversalMethod.LEVITATION
        and route.kind == RouteKind.VERTICAL and route.clear_vertical_path
    ) or (
        method == TraversalMethod.SPIDER_CLIMB
        and route.kind in {RouteKind.VERTICAL, RouteKind.SURFACE} and route.continuous_support
    ) or (
        method == TraversalMethod.SUPERNATURAL_JUMP
        and route.kind in {RouteKind.VERTICAL, RouteKind.GAP}
        and (route.kind != RouteKind.VERTICAL or route.clear_vertical_path)
    ) or (
        method == TraversalMethod.WATER_WALKING
        and route.kind == RouteKind.WATER and route.water_surface
    )
    if not compatible:
        return "route_incompatible", "This ability cannot cross that obstacle using its authored constraints."
    if method == TraversalMethod.SUPERNATURAL_JUMP and (
        mechanics.jump_reach_metres is None or route.distance_metres > mechanics.jump_reach_metres
    ):
        return "route_out_of_reach", "That crossing exceeds this jump's three-metre reach."
    return None, None
