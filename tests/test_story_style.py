from app.config import settings
from app.story import DM_SYSTEM_PROMPT, STYLE_SUFFIX, styled_prompt


def test_styled_prompt_appends_cinematic_style() -> None:
    prompt = styled_prompt("dark fairy-tale tarot card, rusted gates")
    assert prompt.startswith("dark fairy-tale tarot card, rusted gates")
    for mark in ("portal", "cinematic", "no text", "no watermark"):
        assert mark in prompt
    assert not prompt.rstrip().endswith(",")


def test_style_suffix_shared_across_prompts() -> None:
    tail = styled_prompt("a")[-len(STYLE_SUFFIX):]
    assert tail == STYLE_SUFFIX


def test_dm_system_prompt_is_dungeon_master() -> None:
    low = DM_SYSTEM_PROMPT.lower()
    for mark in ("ведущий", "dungeon master", "второе лицо", "дилемма"):
        assert mark in low


def test_story_model_chain_parsing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "story_models", " alpha , beta , ,gamma ")
    assert settings.story_model_chain == ["alpha", "beta", "gamma"]
    monkeypatch.setattr(settings, "story_models", "  ")
    assert settings.story_model_chain == ["openai"]
