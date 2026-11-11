SECTIONS = [
    "핵심 AI 주제",
    "AI 모델 및 서비스",
    "AI Coding Tools",
    "Agent / Multi-Agent",
    "Skills",
    "Plugins / Extensions / Connectors",
    "MCP",
    "Framework / Library",
    "RAG / Knowledge / Memory / Context",
    "Prompt Engineering",
    "API / Tool Calling",
    "Automation",
    "CLI 명령어 및 설정",
    "실전 Workflow",
    "비용·토큰·성능 최적화",
    "비교 내용",
    "실패 사례 / 안티패턴 / 주의사항",
    "Source-specific Unique Insights",
    "Source Citation / Evidence",
]

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "integer", "minimum": 1, "maximum": 18},
                    "text": {"type": "string"},
                    "segment_ids": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": ["demonstrated", "source_claim", "opinion", "uncertain"]},
                },
                "required": ["section", "text", "segment_ids", "kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer", "minimum": 1, "maximum": 18},
                    "markdown": {"type": "string"},
                },
                "required": ["number", "markdown"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "type": {"type": "string", "enum": ["unsupported", "omission", "wrong_value", "wrong_command", "other"]},
                    "detail": {"type": "string"},
                    "segment_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["severity", "type", "detail", "segment_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["issues"],
    "additionalProperties": False,
}
