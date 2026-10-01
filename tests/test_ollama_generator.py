"""
Unit tests for OllamaRuleGenerator (Mocked HTTP).
Verifies provider behavior, strict schema gatekeeping, controlled retries, and error handling.
"""

from unittest.mock import MagicMock, patch
import urllib.error
import pytest

from src.rule_generator import (
    GenerationError,
    OllamaRuleGenerator,
    SchemaValidationError,
    validate_rule_proposal,
)

SAMPLE_HTML = ["<html><body><h2 class='custom-title'>Headline</h2></body></html>"]


def test_ollama_valid_json_response():
    valid_payload = """
    {
        "title": {"selector": "h2.custom-title", "type": "css"},
        "body": {"selector": "div.content", "type": "css"},
        "date": {"selector": "time.post-time", "type": "css"},
        "author": {"selector": "span.byline", "type": "css"}
    }
    """
    gen = OllamaRuleGenerator()
    with patch.object(gen, "_call_ollama_api", return_value=valid_payload) as mock_api:
        rules = gen.generate_rules(SAMPLE_HTML)
        assert mock_api.call_count == 1
        assert rules["title"]["selector"] == "h2.custom-title"
        assert rules["body"]["selector"] == "div.content"
        validate_rule_proposal(rules)


def test_ollama_markdown_fences_cleaned():
    fenced_payload = """
    ```json
    {
        "title": {"selector": "h1.title", "type": "css"},
        "body": {"selector": "div.body", "type": "css"},
        "date": {"selector": "span.date", "type": "css"},
        "author": {"selector": "span.author", "type": "css"}
    }
    ```
    """
    gen = OllamaRuleGenerator()
    with patch.object(gen, "_call_ollama_api", return_value=fenced_payload):
        rules = gen.generate_rules(SAMPLE_HTML)
        assert rules["title"]["selector"] == "h1.title"


def test_ollama_connection_failure():
    gen = OllamaRuleGenerator()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        with pytest.raises(GenerationError, match="connection error"):
            gen.generate_rules(SAMPLE_HTML)


def test_ollama_single_retry_on_malformed_json():
    # First response is malformed, second response is valid JSON
    first_response = "Here is your JSON: { title: bad json }"
    second_response = """
    {
        "title": {"selector": "h1.title", "type": "css"},
        "body": {"selector": "div.body", "type": "css"},
        "date": {"selector": "span.date", "type": "css"},
        "author": {"selector": "span.author", "type": "css"}
    }
    """
    gen = OllamaRuleGenerator()
    with patch.object(gen, "_call_ollama_api", side_effect=[first_response, second_response]) as mock_api:
        rules = gen.generate_rules(SAMPLE_HTML)
        assert mock_api.call_count == 2
        assert gen.retry_occurred is True
        assert rules["title"]["selector"] == "h1.title"


def test_ollama_fails_after_failed_retry_without_mock_fallback():
    # Both responses are invalid
    first_response = "Invalid JSON"
    second_response = "Still Invalid JSON"
    gen = OllamaRuleGenerator()
    with patch.object(gen, "_call_ollama_api", side_effect=[first_response, second_response]):
        with pytest.raises(GenerationError, match="failed strict schema validation after retry"):
            gen.generate_rules(SAMPLE_HTML)


def test_ollama_rejects_disallowed_keys_or_executable_code():
    bad_payload = """
    {
        "title": {"selector": "h1", "type": "css", "exec": "import os; os.system('calc')"}
    }
    """
    gen = OllamaRuleGenerator()
    with patch.object(gen, "_call_ollama_api", return_value=bad_payload):
        with pytest.raises(GenerationError):
            gen.generate_rules(SAMPLE_HTML)


def test_ground_truth_file_not_referenced_by_generator():
    """Ensure OllamaRuleGenerator does not import or load any ground truth file."""
    import inspect
    import src.rule_generator as rg_module

    source = inspect.getsource(rg_module)
    assert "ground_truth" not in source
    assert "modified_portal.yaml" not in source
