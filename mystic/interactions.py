"""Interaction vocabulary helpers.

Direction is who initiated contact. Channel is the concrete communication lane.
Modality is the communication form for the interaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from mystic.types import Channel, Direction, InteractionModality


_CHANNEL_LABELS: dict[Channel, str] = {
    "dashboard": "Dashboard",
    "phone": "Phone",
    "sms": "SMS",
    "cli": "CLI",
}

_MODALITY_LABELS: dict[InteractionModality, str] = {
    "voice": "Voice",
    "text": "Text",
    "mixed": "Mixed",
}

_DIRECTION_LABELS: dict[Direction, str] = {
    "inbound": "Inbound",
    "outbound": "Outbound",
}


@dataclass(slots=True, frozen=True)
class InteractionDescriptor:
    direction: Direction
    direction_label: str
    channel: Channel
    channel_label: str
    modality: InteractionModality
    modality_label: str
    label: str


def describe_call(call: object) -> InteractionDescriptor:
    """Describe a persisted call-like dataclass."""

    return describe_interaction(
        direction=str(getattr(call, "direction", "") or ""),
        channel=str(getattr(call, "channel", "") or ""),
        modality=str(getattr(call, "modality", "") or ""),
    )


def describe_interaction(
    *,
    direction: str | None,
    channel: str | None,
    modality: str | None,
) -> InteractionDescriptor:
    normalized_direction = _normalize_direction(direction)
    normalized_channel = _normalize_channel(channel)
    normalized_modality = _normalize_modality(modality)

    return InteractionDescriptor(
        direction=normalized_direction,
        direction_label=_DIRECTION_LABELS[normalized_direction],
        channel=normalized_channel,
        channel_label=_CHANNEL_LABELS[normalized_channel],
        modality=normalized_modality,
        modality_label=_MODALITY_LABELS[normalized_modality],
        label=_interaction_label(normalized_channel, normalized_modality),
    )


def interaction_payload(descriptor: InteractionDescriptor) -> dict[str, str]:
    return {
        "channel": descriptor.channel,
        "channel_label": descriptor.channel_label,
        "direction_label": descriptor.direction_label,
        "modality": descriptor.modality,
        "modality_label": descriptor.modality_label,
        "interaction_label": descriptor.label,
    }


def interaction_event_payload(descriptor: InteractionDescriptor) -> dict[str, str]:
    payload = interaction_payload(descriptor)
    return {
        "channel": payload["channel"],
        "channelLabel": payload["channel_label"],
        "directionLabel": payload["direction_label"],
        "modality": payload["modality"],
        "modalityLabel": payload["modality_label"],
        "interactionLabel": payload["interaction_label"],
    }


def format_interaction_brief(descriptor: InteractionDescriptor) -> str:
    if descriptor.channel in {"dashboard", "cli"}:
        return descriptor.label
    return f"{descriptor.direction_label} {descriptor.label.lower()}"


def _interaction_label(channel: Channel, modality: InteractionModality) -> str:
    if channel == "dashboard":
        if modality == "text":
            return "Dashboard chat"
        if modality == "voice":
            return "Dashboard voice"
        return "Dashboard mixed"
    if channel == "phone":
        return "Phone call" if modality == "voice" else f"Phone {modality}"
    if channel == "sms":
        return "SMS"
    if channel == "cli":
        return "CLI chat" if modality == "text" else f"CLI {modality}"
    # Exhaustive over the Channel literal; kept for type-checking resilience.
    raise ValueError(f"Unsupported channel: {channel}")


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_direction(value: str | None) -> Direction:
    normalized = _normalize(value)
    if normalized in {"inbound", "outbound"}:
        return normalized  # type: ignore[return-value]
    raise ValueError(f"Unsupported direction: {value!r}")


def _normalize_channel(value: str | None) -> Channel:
    normalized = _normalize(value)
    if normalized in _CHANNEL_LABELS:
        return normalized  # type: ignore[return-value]
    raise ValueError(f"Unsupported channel: {value!r}")


def _normalize_modality(value: str | None) -> InteractionModality:
    normalized = _normalize(value)
    if normalized in _MODALITY_LABELS:
        return normalized  # type: ignore[return-value]
    raise ValueError(f"Unsupported modality: {value!r}")
