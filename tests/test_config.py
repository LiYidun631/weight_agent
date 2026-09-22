from weight_agent.core.config import Settings


def test_chat_runtime_settings_are_configurable() -> None:
    settings = Settings(
        environment="test",
        chat_intent_min_confidence=0.7,
        chat_intent_fallback_confidence=0.82,
        chat_intent_rule_confidence=0.9,
        chat_memory_ttl_hours=12,
        chat_memory_max_turns=6,
        chat_default_timezone="UTC",
        chat_default_locale="en-US",
    )

    assert settings.chat_intent_min_confidence == 0.7
    assert settings.chat_intent_fallback_confidence == 0.82
    assert settings.chat_intent_rule_confidence == 0.9
    assert settings.chat_memory_ttl_hours == 12
    assert settings.chat_memory_max_turns == 6
    assert settings.chat_default_timezone == "UTC"
    assert settings.chat_default_locale == "en-US"
