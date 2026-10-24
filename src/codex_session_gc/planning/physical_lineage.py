# Copyright (c) 2026

"""Normalize complete observed physical provider graphs without selecting effects."""

from codex_session_gc.domain import (
    HistoryMode,
    HistoryProviderReference,
    InvalidGraphError,
    InventoryCompleteness,
    PhysicalInventory,
    RolloutId,
)


def normalize_history_providers(
    inventory: PhysicalInventory,
) -> frozenset[HistoryProviderReference]:
    """Validate metadata coverage and return existing typed physical dependencies.

    Coverage is supplied by the observer, not established by this pure function.
    Prefix coordinates remain metadata observations, without body validation.
    Logical fork, spawn and Guardian relations grant no provider edges here.
    """
    if type(inventory) is not PhysicalInventory:
        message = "Physical lineage requires a typed physical inventory."
        raise TypeError(message)
    if inventory.completeness is not InventoryCompleteness.COMPLETE:
        message = "Unknown namespace coverage cannot establish physical dependencies."
        raise InvalidGraphError(message)
    providers: dict[RolloutId, RolloutId] = {}
    identifiers = {item.ownership.rollout_id for item in inventory.rollouts}
    for item in inventory.rollouts:
        if item.physical is None or item.history_mode is HistoryMode.UNKNOWN:
            message = "Unknown physical metadata cannot establish absence of provider edges."
            raise InvalidGraphError(message)
        prefix = item.history_prefix
        if prefix is None:
            continue
        if item.history_mode is not HistoryMode.PAGINATED:
            message = "A physical prefix contradicts the observed history mode."
            raise InvalidGraphError(message)
        if prefix.provider_rollout_id not in identifiers:
            message = "A physical history provider is missing from the complete inventory."
            raise InvalidGraphError(message)
        providers[item.ownership.rollout_id] = prefix.provider_rollout_id
    _acyclic(identifiers, providers)
    return frozenset(
        HistoryProviderReference(rollout, provider) for rollout, provider in providers.items()
    )


def _acyclic(identifiers: set[RolloutId], providers: dict[RolloutId, RolloutId]) -> None:
    """Visit each immutable rollout at most once, without recursive chain traversal."""
    finished: set[RolloutId] = set()
    for start in identifiers:
        path: set[RolloutId] = set()
        current: RolloutId | None = start
        while current is not None and current not in finished:
            if current in path:
                message = "Physical history dependencies contain a cycle."
                raise InvalidGraphError(message)
            path.add(current)
            current = providers.get(current)
        finished.update(path)
