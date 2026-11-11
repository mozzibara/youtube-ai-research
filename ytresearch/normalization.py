"""Convert model category headings to the pipeline's fixed extraction format."""
from .definitions import SECTIONS
import re


# Explicit aliases for the model's English snake_case category headings.
# First 12 were observed in live Antigravity output; the remaining variants
# are narrowly scoped equivalent headings and must remain unambiguous.
ENGLISH_CATEGORY_ALIASES = {
    "core_ai_topics": 1,
    "ai_models_and_services": 2,
    "ai_coding_tools": 3,
    "agent_multi_agent": 4,
    "skills": 5,
    "plugins_extensions_connectors": 6,
    "mcp": 7,
    "framework_library": 8,
    "rag_knowledge_memory_context": 9,
    "prompt_engineering": 10,
    "api_tool_calling": 11,
    "automation": 12,
    "cli_commands_settings": 13,
    "cli_commands_and_settings": 13,
    "cli_commands_and_config": 13,
    "cli_commands_config": 13,
    "cli_commands_configuration": 13,
    "cli_commands_and_configuration": 13,
    "practical_workflow": 14,
    "practical_workflows": 14,
    "real_world_workflow": 14,
    "real_world_workflows": 14,
    "cost_token_performance_optimization": 15,
    "cost_tokens_performance_optimization": 15,
    "cost_token_performance": 15,
    "comparisons": 16,
    "comparison_content": 16,
    "comparison_details": 16,
    "comparison_points": 16,
    "failure_cases_antipatterns_cautions": 17,
    "failure_cases_anti_patterns_cautions": 17,
    "failure_cases_antipatterns_warnings": 17,
    "source_specific_unique_insights": 18,
    "source_specific_insights": 18,
}


def anchored_unknown_empty_slots(document):
    """Allow an unknown *empty* heading only in a complete, ordered 18-slot map.

    All other 17 headings must independently identify their expected section.
    This never assigns an unknown heading to a claim and never ignores content.
    Return an empty tuple when the narrow exception is not applicable.
    """
    if not isinstance(document, dict) or len(document) != 18:
        return ()
    headings = {f"{i}. {name}": i for i, name in enumerate(SECTIONS[:18], 1)}
    unknown = []
    for position, (key, entries) in enumerate(document.items(), 1):
        known_section = headings.get(key, ENGLISH_CATEGORY_ALIASES.get(key))
        if known_section is None:
            if entries != []:
                return ()
            unknown.append(position)
        elif known_section != position:
            return ()
    return tuple(unknown) if len(unknown) == 1 else ()



def normalize_category_claims(document, schema):
    if not isinstance(document, dict) or not schema:
        return document, False
    if set(schema.get("properties", {})) != {"claims"}:
        return document, False
    if "claims" in document:
        items = document["claims"]
        if not isinstance(items, list):
            return document, False
        headings = {f"{i}. {label}": i for i, label in enumerate(SECTIONS[:18], 1)}
        converted = []
        changed = False
        for item in items:
            if not isinstance(item, dict):
                converted.append(item)
                continue
            value = item.get("section")
            mapped = None
            if isinstance(value, str):
                if re.fullmatch(r"[1-9]|1[0-8]", value.strip()):
                    mapped = int(value.strip())
                elif value.strip() in headings:
                    mapped = headings[value.strip()]
            if mapped is not None:
                item = {**item, "section": mapped}
                changed = True

            # Observed agy variant: {category_id, category_name, claim, segment_ids}.
            # Never infer a section from claim text. Both the integer ID and its
            # canonical name must agree before promoting to the fixed schema.
            if "section" not in item and "category_id" in item:
                if set(item) != {"category_id", "category_name", "claim", "segment_ids"}:
                    raise RuntimeError("AGY_CATEGORY_FIELDS: unexpected or missing claim fields")
                cat_id = item["category_id"]
                cat_name = item["category_name"]
                if type(cat_id) is not int or not 1 <= cat_id <= 18:
                    raise RuntimeError(
                        f"AGY_CATEGORY_ID_INVALID: category_id must be 1..18 integer "
                        f"(type={type(cat_id).__name__}, "
                        f"safe_value={cat_id if type(cat_id) is int and 0 <= cat_id <= 99 else '<redacted>'})"
                    )
                canonical_name = SECTIONS[cat_id - 1]
                valid_names = {canonical_name, f"{cat_id}. {canonical_name}"}
                if not isinstance(cat_name, str) or cat_name.strip() not in valid_names:
                    # Do not print free-form category names from model output.
                    raise RuntimeError(
                        f"AGY_CATEGORY_NAME_MISMATCH: category_id={cat_id}, "
                        "category_name does not match canonical section"
                    )
                claim_text = item["claim"]
                refs = item["segment_ids"]
                if (not isinstance(claim_text, str) or not claim_text.strip()
                        or not isinstance(refs, list) or not refs
                        or any(not isinstance(ref, str) or not ref for ref in refs)):
                    raise RuntimeError("AGY_CATEGORY_FIELDS: claim or segment_ids malformed")
                item = {
                    "section": cat_id, "text": claim_text,
                    "segment_ids": refs, "kind": "source_claim",
                }
                changed = True
            converted.append(item)
        return ({**document, "claims": converted}, True) if changed else (document, False)
    headings = {f"{i}. {name}": i for i, name in enumerate(SECTIONS[:18], 1)}
    if not document:
        return document, False
    # Unknown *nonempty* categories always fail closed. A single unknown
    # *empty* category is allowed only in an otherwise fully anchored, ordered
    # 18-category response, where omitting an empty list loses zero claims.
    unknown_empty_slots = anchored_unknown_empty_slots(document)
    unknown_keys = [
        key for key in document
        if key not in headings and key not in ENGLISH_CATEGORY_ALIASES
    ]
    if unknown_keys and not unknown_empty_slots:
        return document, False
    converted = []
    for position, (heading, entries) in enumerate(document.items(), 1):
        section = headings.get(heading, ENGLISH_CATEGORY_ALIASES.get(heading))
        if section is None:
            # 17 other positions uniquely fix this empty slot; do not make
            # claims or invent category semantics for the unknown label.
            if position in unknown_empty_slots and entries == []:
                continue
            return document, False
        if not isinstance(entries, list):
            # Use only positional index/type, never model-generated field names
            # or potentially sensitive values in errors.
            raise RuntimeError(
                "AGY_CATEGORY_SHAPE: expected list "
                f"(category_position={position}, actual_type={type(entries).__name__})"
            )
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"claim", "segment_ids"}:
                raise RuntimeError("AGY_CATEGORY_SHAPE: expected claim and segment_ids")
            body = entry["claim"]
            refs = entry["segment_ids"]
            if (not isinstance(body, str) or not body.strip()
                    or not isinstance(refs, list) or not refs
                    or any(not isinstance(ref, str) or not ref for ref in refs)):
                raise RuntimeError("AGY_CATEGORY_SHAPE: invalid claim or references")
            converted.append({
                "section": section, "text": body,
                "segment_ids": refs, "kind": "source_claim",
            })
    return {"claims": converted}, True
