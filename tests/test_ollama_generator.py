"""
Unit tests for OllamaRuleGenerator (Mocked HTTP).
Verifies provider behavior, strict schema gatekeeping, controlled retries, and error handling.
"""

from unittest.mock import MagicMock, patch
import pytest
import requests

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
    with patch("requests.post", side_effect=requests.ConnectionError("Connection refused")):
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


def test_ollama_call_api_requests_transport_semantics():
    """Verify requests.post parameters, payload structure, response extraction, and error mappings."""
    gen = OllamaRuleGenerator(
        base_url="http://test-ollama:11434",
        model="custom-model:7b",
        temperature=0.2,
        seed=123,
        timeout=45,
    )

    # 1. Successful POST and response field extraction
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"response": "{\"title\": {\"selector\": \"h1\", \"type\": \"css\"}}"}
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("requests.post", return_value=mock_resp) as mock_post:
        res = gen._call_ollama_api("Test prompt")
        assert res == "{\"title\": {\"selector\": \"h1\", \"type\": \"css\"}}"
        mock_post.assert_called_once_with(
            "http://test-ollama:11434/api/generate",
            json={
                "model": "custom-model:7b",
                "prompt": "Test prompt",
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "seed": 123,
                },
            },
            timeout=45,
        )
        mock_resp.__enter__.assert_called_once()
        mock_resp.__exit__.assert_called_once()

    # 2. HTTP Error (e.g. 500) raises GenerationError
    mock_500 = MagicMock()
    mock_500.status_code = 500
    mock_500.reason = "Internal Server Error"
    mock_500.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    mock_500.__enter__.return_value = mock_500
    mock_500.__exit__.return_value = None

    with patch("requests.post", return_value=mock_500):
        with pytest.raises(GenerationError, match="Ollama API HTTP error at http://test-ollama:11434/api/generate"):
            gen._call_ollama_api("Test prompt")

    # 3. Timeout raises GenerationError with URL context
    with patch("requests.post", side_effect=requests.Timeout("Read timed out")):
        with pytest.raises(GenerationError, match="Ollama request timed out at http://test-ollama:11434/api/generate"):
            gen._call_ollama_api("Test prompt")

    # 4. Malformed JSON response reproduces real requests JSONDecodeError and raises GenerationError
    real_bad_resp = requests.Response()
    real_bad_resp.status_code = 200
    real_bad_resp._content = b"<html><body>Not JSON</body></html>"
    real_bad_resp.encoding = "utf-8"

    # Context manager mock wrapper to assert proper socket/response cleanup
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = real_bad_resp
    mock_ctx.__exit__.return_value = None

    with patch("requests.post", return_value=mock_ctx):
        with pytest.raises(GenerationError) as exc_info:
            gen._call_ollama_api("Test prompt")

        err_msg = str(exc_info.value)
        assert err_msg.startswith("Ollama API request failed: ")
        assert "Expecting value" in err_msg
        assert "Ollama network request failed" not in err_msg
        mock_ctx.__enter__.assert_called_once()
        mock_ctx.__exit__.assert_called_once()
