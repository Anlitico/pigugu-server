# tests/unit/test_personas.py
"""Unit tests for personas  -  PersonaRegistry and individual personas."""

import pytest


class TestPersonaRegistry:
    def test_register_defaults_populates_registry(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        ids = PersonaRegistry.list_ids()
        assert 1 in ids
        assert 2 in ids
        assert 3 in ids

    def test_get_returns_persona(self):
        from system_prompts import PersonaRegistry, Persona
        PersonaRegistry.register_defaults()
        p = PersonaRegistry.get(1)
        assert isinstance(p, Persona)
        assert p.persona_id == 1

    def test_get_unknown_falls_back_to_trump(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        p = PersonaRegistry.get(999)
        assert p.persona_id == 1

    def test_get_by_domain(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        # Trump has domain="politics"
        p = PersonaRegistry.get_by_domain("politics")
        assert p is not None

    def test_register_defaults_is_idempotent(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        PersonaRegistry.register_defaults()
        # Should not duplicate
        ids = PersonaRegistry.list_ids()
        assert len(ids) == 3

    def test_convenience_get_persona(self):
        from system_prompts import get_persona, Persona
        p = get_persona(1)
        assert isinstance(p, Persona)
        assert p.persona_id == 1

    def test_build_prompt_cache_includes_global_prompt(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        cache = PersonaRegistry.build_prompt_cache()
        for pid, prompt in cache.items():
            assert "Pigugu" in prompt
            assert "web_search" in prompt
            assert "list_active_roasts" in prompt
            assert "start_roast" in prompt

    def test_build_prompt_cache_includes_persona_prompt(self):
        from system_prompts import PersonaRegistry
        PersonaRegistry.register_defaults()
        cache = PersonaRegistry.build_prompt_cache()
        assert "Trump" in cache[1]
        assert "Musk" in cache[2]


class TestTrumpPersona:
    def test_basic_fields(self):
        from system_prompts import TrumpPersona
        p = TrumpPersona()
        assert p.persona_id == 1
        assert "Trump" in p.display_name
        assert p.domain == "politics"

    def test_get_full_prompt_returns_string(self):
        from system_prompts import TrumpPersona
        p = TrumpPersona()
        prompt = p.get_full_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100
        assert "Trump" in prompt

    def test_get_full_prompt_volcengine(self):
        from system_prompts import TrumpPersona
        p = TrumpPersona()
        prompt = p.get_full_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_tts_voice_is_set(self):
        from system_prompts import TrumpPersona
        p = TrumpPersona()
        assert p.tts_voice is not None


class TestMuskPersona:
    def test_basic_fields(self):
        from system_prompts import MuskPersona
        p = MuskPersona()
        assert p.persona_id == 2
        assert "Musk" in p.display_name

    def test_get_full_prompt(self):
        from system_prompts import MuskPersona
        p = MuskPersona()
        prompt = p.get_full_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_has_different_domain(self):
        from system_prompts import MuskPersona, TrumpPersona
        m = MuskPersona()
        t = TrumpPersona()
        assert m.domain != t.domain


class TestJamesPersona:
    def test_basic_fields(self):
        from system_prompts import JamesPersona
        p = JamesPersona()
        assert p.persona_id == 3

    def test_get_full_prompt(self):
        from system_prompts import JamesPersona
        p = JamesPersona()
        prompt = p.get_full_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100


class TestPersonaBaseClass:
    def test_is_abstract(self):
        from system_prompts import Persona
        with pytest.raises(TypeError):
            Persona()  # type: ignore[reportAbstractUsage]

    def test_subclass_must_implement_abstract_methods(self):
        from system_prompts import Persona

        class Incomplete(Persona):  # type: ignore[reportAbstractUsage]
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[reportAbstractUsage]
