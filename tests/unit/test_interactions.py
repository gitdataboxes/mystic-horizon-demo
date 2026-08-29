from __future__ import annotations

from types import SimpleNamespace

from mystic.interactions import describe_call, describe_interaction, format_interaction_brief


def test_dashboard_chat_is_explicit_channel() -> None:
    descriptor = describe_interaction(
        direction="inbound",
        channel="dashboard",
        modality="text",
    )

    assert descriptor.channel == "dashboard"
    assert descriptor.channel_label == "Dashboard"
    assert descriptor.direction == "inbound"
    assert descriptor.modality == "text"
    assert descriptor.label == "Dashboard chat"


def test_phone_voice_formats_as_phone_call() -> None:
    descriptor = describe_call(
        SimpleNamespace(
            id="call-1",
            direction="outbound",
            channel="phone",
            modality="voice",
            audience="public",
            external_id="CA123",
        )
    )

    assert descriptor.channel == "phone"
    assert descriptor.channel_label == "Phone"
    assert descriptor.direction_label == "Outbound"
    assert format_interaction_brief(descriptor) == "Outbound phone call"


def test_dashboard_voice_label_is_derived_from_channel_and_modality() -> None:
    descriptor = describe_interaction(
        direction="inbound",
        channel="dashboard",
        modality="voice",
    )

    assert descriptor.channel == "dashboard"
    assert descriptor.channel_label == "Dashboard"
    assert descriptor.direction == "inbound"
    assert descriptor.modality == "voice"
    assert descriptor.label == "Dashboard voice"
