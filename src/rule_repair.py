"""
Rule Repair module for CorpusAI.

Coordinates failure detection on changed website markup, candidate generation via RuleGenerator,
threshold validation via SelectorValidator, and versioned rule history tracking behind an
explicit human approval boundary.
"""

from copy import deepcopy
from typing import Any, Dict, List, Optional, Union
from src.extractor import DeterministicExtractor
from src.rule_generator import RuleGenerator, MockRuleGenerator, validate_rule_proposal
from src.selector_validator import SelectorValidator, DEFAULT_SELECTOR_THRESHOLD


class RuleRepairManager:
    """
    Manages detection of layout breakage, candidate rule proposal, validation gatekeeping,
    and history-preserving rule configuration updates.
    """

    def __init__(
        self,
        validator: Optional[SelectorValidator] = None,
        generator: Optional[RuleGenerator] = None,
        extractor: Optional[DeterministicExtractor] = None,
    ):
        self.validator = validator or SelectorValidator(threshold=DEFAULT_SELECTOR_THRESHOLD)
        self.generator = generator or MockRuleGenerator()
        self.extractor = extractor or DeterministicExtractor()

    def detect_failure(self, extraction_result: Dict[str, Any]) -> bool:
        """
        Check if a single document's extraction result contains any field failures.
        """
        return not all(field.get("success", False) for field in extraction_result.values())

    def detect_failures(
        self,
        html_samples: List[str],
        current_rules: Dict[str, Union[str, Dict[str, Any]]],
    ) -> List[str]:
        """
        Evaluate current extraction rules across representative HTML samples and identify
        which specific fields fail to extract reliably.

        Returns:
            List of failing field names.
        """
        failed_fields: List[str] = []
        if not html_samples:
            return failed_fields

        for field_name, rule_def in current_rules.items():
            report = self.validator.validate_selector(html_samples, rule_def)
            if not report["is_acceptable"]:
                failed_fields.append(field_name)

        return failed_fields

    def propose_repairs(
        self,
        html_samples: List[str],
        current_rules: Dict[str, Union[str, Dict[str, Any]]],
        failed_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Identify failing fields, generate candidate replacement rules, and validate each candidate.

        Pipeline:
        1. Identify failing fields (if not explicitly provided).
        2. Propose candidate replacement selectors using RuleGenerator.
        3. Validate candidate proposals against strict schema.
        4. Validate candidate selectors against html_samples via SelectorValidator.
        5. Return detailed proposals without modifying current configuration.
        """
        if failed_fields is None:
            failed_fields = self.detect_failures(html_samples, current_rules)

        if not failed_fields:
            return {
                "failed_fields": [],
                "proposals": {},
                "all_accepted": True,
                "message": "No failed fields detected. No repair needed.",
            }

        # Step 2: Generate candidate rules for failed fields
        candidate_rules = self.generator.generate_rules(html_samples, failed_fields=failed_fields)

        # Step 3: Validate schema
        validate_rule_proposal(candidate_rules)

        # Step 4: Validate each candidate against representative samples
        proposals: Dict[str, Dict[str, Any]] = {}
        all_accepted = True

        for field in failed_fields:
            if field not in candidate_rules:
                continue

            cand = candidate_rules[field]
            val_report = self.validator.validate_selector(html_samples, cand)

            old_rule = current_rules.get(field, {})
            if isinstance(old_rule, dict):
                old_sel = old_rule.get("selector", "")
                old_type = old_rule.get("type", "css")
            else:
                old_sel = str(old_rule)
                old_type = "css"

            is_accepted = bool(val_report["is_acceptable"])
            if not is_accepted:
                all_accepted = False

            proposals[field] = {
                "field": field,
                "old_selector": old_sel,
                "old_type": old_type,
                "candidate_selector": cand["selector"],
                "candidate_type": cand.get("type", "css"),
                "success_rate": val_report["success_rate"],
                "threshold": self.validator.threshold,
                "accepted": is_accepted,
                "validation_details": val_report,
            }

        return {
            "failed_fields": failed_fields,
            "proposals": proposals,
            "all_accepted": all_accepted,
            "tested_pages": len(html_samples),
        }

    def apply_repairs(
        self,
        current_config: Dict[str, Any],
        repair_proposals: Dict[str, Any],
        approved: bool = False,
        source: str = "mock-ai",
        reason: str = "layout change detected",
    ) -> Dict[str, Any]:
        """
        Apply approved candidate repairs to the configuration with full version and history tracking.

        Args:
            current_config: The current portal configuration dict (with 'selectors' and 'metadata').
            repair_proposals: Output from propose_repairs().
            approved: Explicit human approval gate. If False, raises PermissionError.
            source: Rule source provenance ('mock-ai', 'ai-assisted', 'manual').
            reason: Explanation for rule change.

        Returns:
            Updated configuration dictionary with new current selectors and preserved history.
        """
        if not approved:
            raise PermissionError(
                "Cannot apply repairs without explicit approval (approved=True required)."
            )

        updated_config = deepcopy(current_config)
        selectors_dict = updated_config.setdefault("selectors", {})
        metadata_dict = updated_config.setdefault("metadata", {})

        current_ver = metadata_dict.get("version", 1)
        proposals = repair_proposals.get("proposals", {})

        applied_count = 0
        for field, prop in proposals.items():
            if not prop.get("accepted", False):
                # Never apply rejected proposals
                continue

            cand_sel = prop["candidate_selector"]
            cand_type = prop["candidate_type"]
            score = prop["success_rate"]

            current_field_rule = selectors_dict.get(field, {})

            # Normalize current rule representation
            if isinstance(current_field_rule, str):
                old_sel = current_field_rule
                old_type = "css"
                history_list = []
                field_ver = current_ver
            elif isinstance(current_field_rule, dict):
                # May have nested 'current' or flat 'selector'
                if "current" in current_field_rule and isinstance(current_field_rule["current"], dict):
                    old_sel = current_field_rule["current"].get("selector", "")
                    old_type = current_field_rule["current"].get("type", "css")
                else:
                    old_sel = current_field_rule.get("selector", "")
                    old_type = current_field_rule.get("type", "css")
                history_list = current_field_rule.get("history", [])
                field_ver = current_field_rule.get("version", current_ver)
            else:
                old_sel = ""
                old_type = "css"
                history_list = []
                field_ver = current_ver

            # Append old rule to history
            history_list.append({
                "version": field_ver,
                "selector": old_sel,
                "type": old_type,
                "source": metadata_dict.get("created_by", "manual"),
                "reason": "initial rule" if field_ver == 1 else "prior rule version",
            })

            new_field_ver = field_ver + 1

            # Update to new rule with history
            selectors_dict[field] = {
                "selector": cand_sel,
                "type": cand_type,
                "version": new_field_ver,
                "source": source,
                "reason": reason,
                "validation_score": score,
                "history": history_list,
            }
            applied_count += 1

        if applied_count > 0:
            metadata_dict["version"] = current_ver + 1
            metadata_dict["updated_by"] = source
            metadata_dict["validated"] = True

        return updated_config
