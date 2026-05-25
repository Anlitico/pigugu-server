"""Tests for roast registry and mode resolution."""

from roast import GameModeRegistry, get_game_mode  # pyright: ignore[reportAttributeAccessIssue]
from roast.base import GameMode
from roast.types import Mode


class TestGameModeRegistry:
    def test_register_defaults(self):
        GameModeRegistry.register_defaults()
        assert Mode.ROAST_TOGETHER in GameModeRegistry._modes
        assert Mode.DEBATE_BICKER in GameModeRegistry._modes
        assert Mode.BREAKING_BOMB in GameModeRegistry._modes

    def test_get_default(self):
        m = get_game_mode("roast_together")
        assert isinstance(m, GameMode)
        assert m.mode == Mode.ROAST_TOGETHER

    def test_get_debate(self):
        m = get_game_mode("debate_bicker")
        assert m.mode == Mode.DEBATE_BICKER

    def test_get_unknown_falls_back(self):
        m = get_game_mode("nonexistent")
        assert m.mode == Mode.ROAST_TOGETHER

    def test_get_via_enum(self):
        m = get_game_mode(Mode.BREAKING_BOMB)
        assert m.mode == Mode.BREAKING_BOMB

    def test_all_have_extensions(self):
        for mode in [Mode.ROAST_TOGETHER, Mode.DEBATE_BICKER, Mode.BREAKING_BOMB]:
            m = get_game_mode(mode)
            assert m.system_prompt_extension, f"{mode} has no prompt extension"

    def test_all_have_max_turns(self):
        for mode in [Mode.ROAST_TOGETHER, Mode.DEBATE_BICKER, Mode.BREAKING_BOMB]:
            m = get_game_mode(mode)
            assert m.max_turns > 0, f"{mode} has no max_turns"
