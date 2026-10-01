"""
Rule Generator module for CorpusAI.

Provides:
- RuleGenerator interface
- Strict schema validation for candidate rules (validate_rule_proposal)
- MockRuleGenerator for deterministic unit testing
- OllamaRuleGenerator for real local LLM-assisted selector generation
"""

from abc import ABC, abstractmethod
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set
import urllib.error
import urllib.request

from src.html_preprocessor import preprocess_html

ALLOWED_SELECTOR_TYPES: Set[str] = {"css", "xpath"}
ALLOWED_RULE_KEYS: Set[str] = {"selector", "type", "description"}


class SchemaValidationError(ValueError):
    """Raised when a candidate extraction rule fails strict schema validation."""
    pass


class GenerationError(RuntimeError):
    """Raised when an LLM generation fails due to network, format, or schema errors."""
    pass


def validate_rule_proposal(proposal: Dict[str, Any]) -> None:
    """
    Validate candidate rule proposals against strict schema constraints.

    Rejects:
    - Non-dictionary structures
    - Missing or empty selector strings
    - Non-string selector values
    - Unsupported selector types (only 'css' and 'xpath' allowed)
    - Disallowed dictionary keys
    - Dangerous / executable code tokens

    Args:
        proposal: Mapping of field_name -> {"selector": "...", "type": "..."}

    Raises:
        SchemaValidationError: If any rule in the proposal violates constraints.
    """
    if not isinstance(proposal, dict):
        raise SchemaValidationError(f"Rule proposal must be a dict, got {type(proposal).__name__}")

    if not proposal:
        raise SchemaValidationError("Rule proposal cannot be empty")

    for field, rule in proposal.items():
        if not isinstance(field, str) or not field.strip():
            raise SchemaValidationError(f"Invalid field name: {field!r}")

        if not isinstance(rule, dict):
            raise SchemaValidationError(f"Rule for field '{field}' must be a dict, got {type(rule).__name__}")

        # Check keys
        extra_keys = set(rule.keys()) - ALLOWED_RULE_KEYS
        if extra_keys:
            raise SchemaValidationError(
                f"Field '{field}' contains disallowed keys: {sorted(extra_keys)}"
            )

        if "selector" not in rule:
            raise SchemaValidationError(f"Field '{field}' is missing required 'selector' key")

        selector = rule["selector"]
        if not isinstance(selector, str):
            raise SchemaValidationError(
                f"Field '{field}' selector must be a string, got {type(selector).__name__}"
            )

        if not selector.strip():
            raise SchemaValidationError(f"Field '{field}' selector cannot be empty or whitespace")

        sel_type = rule.get("type", "css")
        if not isinstance(sel_type, str):
            raise SchemaValidationError(
                f"Field '{field}' selector type must be a string, got {type(sel_type).__name__}"
            )

        if sel_type.lower() not in ALLOWED_SELECTOR_TYPES:
            raise SchemaValidationError(
                f"Field '{field}' specifies unsupported selector type '{sel_type}'. Allowed: {sorted(ALLOWED_SELECTOR_TYPES)}"
            )

        # Safety: reject common code injection / execution tokens
        dangerous_patterns = [r"\b__\w+__\b", r"\bimport\b", r"\beval\b", r"\bexec\b", r"<script"]
        for pat in dangerous_patterns:
            if re.search(pat, selector, re.IGNORECASE):
                raise SchemaValidationError(
                    f"Field '{field}' contains dangerous or disallowed pattern: {selector}"
                )


def clean_json_response(raw_text: str) -> str:
    """
    Clean potential markdown formatting, fences, or commentary from raw LLM output.
    """
    text = raw_text.strip()
    # Match markdown code block ```json ... ``` or ``` ... ```
    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if code_block_match:
        text = code_block_match.group(1).strip()
    return text


class RuleGenerator(ABC):
    """
    Abstract Base Class for extraction rule generators.
    """

    @abstractmethod
    def generate_rules(
        self,
        html_samples: List[str],
        failed_fields: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, str]]:
        """
        Generate candidate selectors given representative HTML samples and optional failed fields.

        Args:
            html_samples: List of representative HTML sample strings.
            failed_fields: Optional list of field names that require repair.

        Returns:
            Dict mapping field_name -> {"selector": "...", "type": "css"|"xpath"}
        """
        pass


class MockRuleGenerator(RuleGenerator):
    """
    Deterministic mock rule generator for unit testing without LLM dependencies.
    """

    DEFAULT_REDESIGN_RULES: Dict[str, Dict[str, str]] = {
        "title": {"selector": "h2.headline", "type": "css"},
        "body": {"selector": "section.story-text", "type": "css"},
        "date": {"selector": "span.post-timestamp", "type": "css"},
        "author": {"selector": "span.byline-author", "type": "css"},
    }

    def __init__(self, custom_proposals: Optional[Dict[str, Dict[str, str]]] = None):
        self.proposals = custom_proposals if custom_proposals is not None else self.DEFAULT_REDESIGN_RULES

    def generate_rules(
        self,
        html_samples: List[str],
        failed_fields: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, str]]:
        if not html_samples:
            raise ValueError("html_samples cannot be empty")

        target_fields = failed_fields if failed_fields is not None else list(self.proposals.keys())

        result: Dict[str, Dict[str, str]] = {}
        for field in target_fields:
            if field in self.proposals:
                result[field] = dict(self.proposals[field])
            else:
                result[field] = {"selector": f".{field}", "type": "css"}

        validate_rule_proposal(result)
        return result


class OllamaRuleGenerator(RuleGenerator):
    """
    Real local LLM provider using Ollama HTTP API.

    Inspects preprocessed HTML samples and infers CSS/XPath selectors.
    Operates with strict schema validation and a single controlled formatting retry.
    """

    DEFAULT_PROMPT_PATH = os.path.join(
        os.path.dirname(__file__), "..", "config", "prompts", "selector_generation.txt"
    )

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        seed: int = 42,
        timeout: int = 120,
        prompt_template_path: Optional[str] = None,
    ):
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("CORPUSAI_MODEL", "qwen2.5-coder:3b")
        self.temperature = temperature
        self.seed = seed
        self.timeout = timeout
        self.prompt_template_path = prompt_template_path or self.DEFAULT_PROMPT_PATH
        self.prompt_version = "1.0"

        # Telemetry & raw response tracking
        self.last_raw_response: Optional[str] = None
        self.last_latency_seconds: float = 0.0
        self.retry_occurred: bool = False

    def _call_ollama_api(self, prompt: str) -> str:
        """
        Send a generate request to the Ollama HTTP API endpoint.
        """
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result_json = json.loads(resp.read().decode("utf-8"))
                return result_json.get("response", "")
        except urllib.error.URLError as e:
            raise GenerationError(f"Ollama connection error at {url}: {e.reason}")
        except Exception as e:
            raise GenerationError(f"Ollama API request failed: {str(e)}")

    def _format_prompt(self, html_samples: List[str]) -> str:
        """
        Build prompt incorporating preprocessed HTML sample blocks.
        """
        if os.path.exists(self.prompt_template_path):
            with open(self.prompt_template_path, "r", encoding="utf-8") as f:
                template = f.read()
        else:
            # Safe default template
            template = (
                "Infer CSS selectors for title, body, date, author from the following HTML pages.\n"
                "Return valid JSON only.\n\n{html_samples_block}"
            )

        samples_text = ""
        for i, raw_html in enumerate(html_samples, start=1):
            cleaned = preprocess_html(raw_html)
            samples_text += f"\n--- SAMPLE PAGE {i} ---\n{cleaned}\n"

        return template.replace("{html_samples_block}", samples_text)

    def generate_rules(
        self,
        html_samples: List[str],
        failed_fields: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, str]]:
        """
        Execute real LLM selector generation using Ollama with strict schema validation.

        Args:
            html_samples: List of representative HTML sample strings.
            failed_fields: Optional list of field names that need repair.

        Returns:
            Dict mapping field_name -> {"selector": "...", "type": "css"|"xpath"}

        Raises:
            GenerationError: If LLM is unreachable, returns malformed JSON, or fails schema validation.
        """
        if not html_samples:
            raise ValueError("html_samples cannot be empty")

        prompt = self._format_prompt(html_samples)
        self.retry_occurred = False

        start_time = time.time()
        raw_response = self._call_ollama_api(prompt)
        self.last_latency_seconds = round(time.time() - start_time, 3)
        self.last_raw_response = raw_response

        # Parse and validate response
        cleaned_text = clean_json_response(raw_response)
        proposal_dict: Optional[Dict[str, Any]] = None

        try:
            proposal_dict = json.loads(cleaned_text)
            validate_rule_proposal(proposal_dict)
        except (json.JSONDecodeError, SchemaValidationError) as first_err:
            # Allow at most ONE controlled formatting retry without revealing any expected selectors
            self.retry_occurred = True
            retry_prompt = (
                f"{prompt}\n\n"
                "ERROR: Your previous response did not satisfy the required JSON schema.\n"
                "Return ONLY a valid JSON object matching the requested schema with no commentary or markdown."
            )
            retry_raw = self._call_ollama_api(retry_prompt)
            self.last_raw_response = retry_raw
            cleaned_retry = clean_json_response(retry_raw)

            try:
                proposal_dict = json.loads(cleaned_retry)
                validate_rule_proposal(proposal_dict)
            except Exception as second_err:
                raise GenerationError(
                    f"LLM output failed strict schema validation after retry: {second_err}"
                ) from second_err

        # Filter fields if failed_fields specified
        if failed_fields is not None:
            filtered = {k: v for k, v in proposal_dict.items() if k in failed_fields}
            return filtered

        return proposal_dict
