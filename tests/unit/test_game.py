from __future__ import annotations

from mystic.game import (
    ALARM_EVENTS,
    LIGHTNING_UNLOCK_WAVE,
    SHIELD_COOLDOWN_WAVES,
    SHIELD_MAX_PARTICLES,
    VALID_SHOT_PATTERNS,
    VOICE_WEAPON_UNLOCK_WAVE,
    GameState,
    apply_game_tick,
    build_game_tools,
    change_shot_pattern,
    enable_force_field,
    enable_shield,
    engage_shift_jump,
    fire_lightning_weapon,
    fire_voice_weapon,
    format_game_event_cue,
    format_ship_status,
    parse_game_packet,
    read_ship_status,
)


# ── Speak-event classification ──────────────────────────────────────────────


def test_speak_events_cover_alarms_and_flow() -> None:
    expected = {
        # core alarms
        "system_boot",
        "shield_online",
        "shield_refused",
        "shield_depleted",
        "shift_jump_refused",
        "force_field_refused",
        "lightning_weapon_refused",
        "voice_weapon_refused",
        "hull_lost",
        "ship_destroyed",
        "game_over",
        # flow cues — Copilot is allowed to rattle
        "wave_cleared",
        "kill_streak",
        "near_miss",
        "high_score",
        "wave_stalled",
        "idle",
    }
    assert set(ALARM_EVENTS) == expected


def test_game_tick_is_silent() -> None:
    # game_tick mutates state cache only — never speaks.
    assert "game_tick" not in ALARM_EVENTS


# ── Cue formatter ───────────────────────────────────────────────────────────


def test_cue_is_bracketed_uppercase_event() -> None:
    cue = format_game_event_cue("system_boot", {})
    assert cue.startswith("[SYSTEM_BOOT]")


def test_cue_emits_known_payload_keys() -> None:
    cue = format_game_event_cue("shield_refused", {"waves": 2})
    assert cue == "[SHIELD_REFUSED] waves=2"


def test_cue_emits_lives_remaining_for_hull_lost() -> None:
    assert format_game_event_cue("hull_lost", {"lives_remaining": 1}) == (
        "[HULL_LOST] lives_remaining=1"
    )
    assert format_game_event_cue("hull_lost", {"lives_remaining": 2}) == (
        "[HULL_LOST] lives_remaining=2"
    )


def test_cue_emits_reason_and_unlock_wave_for_weapon_refusals() -> None:
    cue = format_game_event_cue(
        "lightning_weapon_refused",
        {"reason": "locked", "unlock_wave": LIGHTNING_UNLOCK_WAVE},
    )
    assert "reason=locked" in cue
    assert f"unlock_wave={LIGHTNING_UNLOCK_WAVE}" in cue
    assert cue.startswith("[LIGHTNING_WEAPON_REFUSED]")


def test_cue_emits_no_target_reason() -> None:
    assert format_game_event_cue(
        "lightning_weapon_refused", {"reason": "no_target"}
    ) == "[LIGHTNING_WEAPON_REFUSED] reason=no_target"


def test_cue_for_voice_weapon_locked() -> None:
    cue = format_game_event_cue(
        "voice_weapon_refused",
        {"reason": "locked", "unlock_wave": VOICE_WEAPON_UNLOCK_WAVE},
    )
    assert "reason=locked" in cue
    assert f"unlock_wave={VOICE_WEAPON_UNLOCK_WAVE}" in cue


def test_cue_game_over_includes_score_wave_rank_prev_best_and_ghosts() -> None:
    cue = format_game_event_cue(
        "game_over",
        {
            "score": 8500,
            "wave": 4,
            "rank": 2,
            "prev_best": 9500,
            "beat_names": ["abc", "XYZ", "", "  bell  "],
        },
    )
    assert cue.startswith("[GAME_OVER]")
    assert "score=8500" in cue
    assert "wave=4" in cue
    assert "rank=2" in cue
    assert "prev_best=9500" in cue
    # ghosts: uppercased, blanks dropped, capped at 3, comma-joined
    assert "ghosts=ABC,XYZ,BELL" in cue


def test_cue_game_over_omits_missing_fields() -> None:
    cue = format_game_event_cue("game_over", {"score": 1200, "wave": 2})
    assert cue == "[GAME_OVER] wave=2 score=1200"


def test_cue_game_over_omits_empty_ghosts() -> None:
    cue = format_game_event_cue(
        "game_over", {"score": 100, "wave": 1, "beat_names": ["", "  "]}
    )
    assert "ghosts=" not in cue


def test_cue_flow_events_have_no_required_payload() -> None:
    # Flow cues like near_miss may carry no payload. The bracket stays.
    assert format_game_event_cue("near_miss", {}) == "[NEAR_MISS]"
    assert format_game_event_cue("idle", {}) == "[IDLE]"


def test_cue_kill_streak_includes_count() -> None:
    assert format_game_event_cue("kill_streak", {"count": 8}) == (
        "[KILL_STREAK] count=8"
    )


def test_cue_wave_cleared_includes_wave_and_next_wave() -> None:
    cue = format_game_event_cue("wave_cleared", {"wave": 3, "next_wave": 4})
    assert cue.startswith("[WAVE_CLEARED]")
    assert "wave=3" in cue
    assert "next_wave=4" in cue


def test_cue_drops_unknown_keys() -> None:
    # Only keys in _CUE_KEY_ORDER (and beat_names) are emitted — keeps the
    # cue short and the LLM unconfused by unrelated payload fields.
    cue = format_game_event_cue(
        "shield_refused", {"waves": 1, "noise": "ignored", "extra": 999}
    )
    assert cue == "[SHIELD_REFUSED] waves=1"


def test_cue_empty_event_type_returns_empty_string() -> None:
    assert format_game_event_cue("", {}) == ""


# ── GameState cache ─────────────────────────────────────────────────────────


def test_game_state_defaults() -> None:
    state = GameState()
    assert state.wave == 1
    assert state.shot_pattern == "single"
    assert state.shield_status == "available"


def test_shield_status_active() -> None:
    state = GameState(shield_particles=7)
    assert "active (7 particles)" in state.shield_status


def test_shield_status_recharging() -> None:
    state = GameState()
    state.arm_shield()
    assert state.shield_cooldown_waves == SHIELD_COOLDOWN_WAVES
    assert "recharging" in state.shield_status


def test_bump_cooldown_decrements_to_zero_only() -> None:
    state = GameState()
    state.arm_shield()
    state.bump_cooldown()
    assert state.shield_cooldown_waves == SHIELD_COOLDOWN_WAVES - 1
    for _ in range(10):
        state.bump_cooldown()
    assert state.shield_cooldown_waves == 0


def test_apply_game_tick_merges_fields() -> None:
    state = GameState()
    apply_game_tick(
        state,
        {
            "wave": 3,
            "score": 1200,
            "lives": 2,
            "asteroids": 6,
            "shield_particles": 4,
            "force_field_active": True,
            "lightning_cooldown_ms": 2400,
            "voice_weapon_cooldown_ms": 0,
            "shot_pattern": "spread",
        },
    )
    assert (state.wave, state.score, state.lives, state.asteroids) == (3, 1200, 2, 6)
    assert state.shield_particles == 4
    assert state.force_field_active is True
    assert state.lightning_cooldown_ms == 2400
    assert state.shot_pattern == "spread"


def test_apply_game_tick_rejects_invalid_pattern() -> None:
    state = GameState(shot_pattern="single")
    apply_game_tick(state, {"shot_pattern": "laser"})
    assert state.shot_pattern == "single"


def test_apply_game_tick_ignores_wrong_types() -> None:
    state = GameState(wave=5)
    apply_game_tick(state, {"wave": "bogus", "lives": None})
    assert state.wave == 5


# ── read_ship_status renderer ───────────────────────────────────────────────


def test_format_ship_status_is_one_line_and_covers_fields() -> None:
    state = GameState(wave=3, score=2400, lives=2, asteroids=5, shield_particles=7)
    summary = format_ship_status(state)

    assert "\n" not in summary
    assert "wave 3" in summary
    assert "lives 2" in summary
    assert "score 2400" in summary
    assert "field 5 rocks" in summary
    assert "shield: active" in summary
    assert "force field: offline" in summary
    assert "lightning: available" in summary
    assert "voice weapon: available" in summary
    assert "single" in summary  # default shot pattern


def test_format_ship_status_reflects_cooldown() -> None:
    state = GameState()
    state.arm_shield()
    assert "recharging" in format_ship_status(state)


def test_format_ship_status_reflects_weapon_locks_and_cooldowns() -> None:
    locked = GameState(wave=1)
    assert "lightning: available" in format_ship_status(locked)
    assert f"voice weapon: locked until wave {VOICE_WEAPON_UNLOCK_WAVE}" in format_ship_status(locked)

    cooling = GameState(
        wave=max(LIGHTNING_UNLOCK_WAVE, VOICE_WEAPON_UNLOCK_WAVE),
        lightning_cooldown_ms=5100,
        voice_weapon_cooldown_ms=1200,
    )
    summary = format_ship_status(cooling)
    assert "lightning: cooling" in summary
    assert "voice weapon: cooling" in summary


# ── Packet parser ───────────────────────────────────────────────────────────


def test_parse_game_packet_happy_path() -> None:
    assert parse_game_packet(b'{"type":"game_tick","payload":{"wave":3}}') == (
        "game_tick",
        {"wave": 3},
    )


def test_parse_game_packet_missing_payload_defaults_to_empty_dict() -> None:
    assert parse_game_packet(b'{"type":"hull_lost"}') == ("hull_lost", {})


def test_parse_game_packet_rejects_invalid() -> None:
    assert parse_game_packet(b"not json") is None
    assert parse_game_packet(b"[1,2,3]") is None
    assert parse_game_packet(b'{"payload":{}}') is None
    assert parse_game_packet(b'{"type":""}') is None


# ── Tool surface ────────────────────────────────────────────────────────────


def test_build_game_tools_returns_full_copilot_kit() -> None:
    tools = build_game_tools()
    names = {getattr(t, "name", None) or getattr(t, "__name__", None) for t in tools}
    assert names == {
        "enable_shield",
        "engage_shift_jump",
        "enable_force_field",
        "fire_lightning_weapon",
        "fire_voice_weapon",
        "change_shot_pattern",
        "read_ship_status",
    }


def test_every_tool_is_a_function_tool() -> None:
    for tool in (
        enable_shield,
        engage_shift_jump,
        enable_force_field,
        fire_lightning_weapon,
        fire_voice_weapon,
        change_shot_pattern,
        read_ship_status,
    ):
        assert tool is not None


def test_shot_pattern_vocabulary_contract() -> None:
    assert VALID_SHOT_PATTERNS == ("single", "spread", "rapid")


def test_shield_particle_count_matches_atomic_orbit() -> None:
    # 5 shells × 2 motes = 10 matches the graph's drawPersonOrbital default.
    assert SHIELD_MAX_PARTICLES == 10
