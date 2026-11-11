"""Phase 4: evidence-preserving 19-section research report draft assembly.

No model calls. This compiler never converts model-reported text into fact
or treats numeric claims in source-generated media as independently verified.
Optional structured research_items are checked against actual source references.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
from typing import Any, Dict, List

from .evidence_refs import reconcile_analysis_evidence
from .claim_review import review_research_claim
from .quality_gate import assess_research_run
from .research_state import open_research_db, RUN_ID, VIDEO_ID
from .storage import atomic_write, json_text, read_json


SECTION_TITLES = (
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
)


class ResearchReportBlocked(ValueError):
    """Inputs are structurally invalid or report has already been approved."""


def _seconds(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _timestamp(value: Any) -> str:
    if isinstance(value, str) and ":" in value:
        return value
    sec = max(0, int(_seconds(value)))
    return f"{sec//60:02d}:{sec%60:02d}"


def _inline(value: Any, *, limit: int = 2800) -> str:
    """Keep extracted content as inline text without creating fake headings."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:limit] + (" [TRUNCATED_FOR_REPORT]" if len(text) > limit else "")


def _evidence_label(ref: Any, segment_ids=None) -> str:
    parts = []
    if ref:
        parts.append(_inline(ref, limit=800))
    if segment_ids:
        parts.extend(str(x) for x in segment_ids)
    return ", ".join(parts) if parts else "원본 근거 위치 미지정 — 별도 검수 필요"


def _validated_research_items(
    analysis: Dict[str, Any], frames: Dict[str, Any], subtitles: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Reject invented section IDs, panel references and subtitle IDs."""
    possible = analysis.get("research_items", []) or []
    if not isinstance(possible, list):
        raise ResearchReportBlocked("RESEARCH_ITEMS_NOT_ARRAY")
    known_segments = {
        str(s.get("id") or s.get("segment_id"))
        for s in subtitles.get("segments", []) or []
    }
    refs, clean = [], []
    for index, raw in enumerate(possible, 1):
        if not isinstance(raw, dict):
            raise ResearchReportBlocked(f"RESEARCH_ITEM_NOT_OBJECT:{index}")
        section_id = raw.get("section_id")
        if type(section_id) is not int or not 1 <= section_id <= 18:
            raise ResearchReportBlocked(f"INVALID_RESEARCH_SECTION:{index}")
        value = str(raw.get("text") or "").strip()
        if not value:
            raise ResearchReportBlocked(f"RESEARCH_ITEM_EMPTY:{index}")
        segments = raw.get("source_segment_ids") or []
        if not isinstance(segments, list):
            raise ResearchReportBlocked(f"RESEARCH_SEGMENTS_NOT_ARRAY:{index}")
        if any(str(seg) not in known_segments for seg in segments):
            raise ResearchReportBlocked(f"UNKNOWN_RESEARCH_SEGMENT:{index}")
        ref = str(raw.get("evidence_ref") or "")
        if not ref and not segments:
            raise ResearchReportBlocked(f"RESEARCH_ITEM_WITHOUT_SOURCE:{index}")
        if ref:
            refs.append({"evidence_ref": ref})
        clean.append({
            "section_id": section_id, "text": value,
            "timestamp": raw.get("timestamp", ""),
            "source_segment_ids": segments, "evidence_ref": ref,
            "claim_origin": raw.get("claim_origin"),
            "source_context": raw.get("source_context"),
        })
    if refs:
        reconcile_analysis_evidence({"prompts_extracted": refs}, frames)
    return clean


def _collect_sections(analysis: Dict[str, Any], frames: Dict[str, Any],
                      subtitles: Dict[str, Any], audited_claims=None):
    """Assemble research candidates with attribution; never assert verified facts."""
    sections: Dict[int, List[str]] = {i: [] for i in range(1, 20)}
    items = _validated_research_items(analysis, frames, subtitles)
    audited = {
        row["item_index"]: row for row in
        (audited_claims or {}).get("items", [])
    }
    claim_flags: Dict[str, int] = {}
    priority_counts = {"P0": 0, "P1": 0, "P2": 0}
    for n, item in enumerate(items, 1):
        detail = review_research_claim(item, frames)
        for flag in detail["review_flags"]:
            claim_flags[flag] = claim_flags.get(flag, 0) + 1
        priority_counts[detail["review_priority"]] += 1
        loc = _evidence_label(item["evidence_ref"], item["source_segment_ids"])
        origin = detail["claim_origin"]
        risk_list = ", ".join(detail["risk_signals"]) or "별도 내용 위험 신호 없음"
        gaps = ", ".join(detail["verification_gaps"]) or "추가 검증 필요"
        contextual = ", ".join(detail["context_flags"])
        adjudication = audited.get(n, {})
        audit_status = adjudication.get("finding_status", "NOT_REVIEWED")
        audit_match = adjudication.get("source_match", "NOT_REVIEWED")
        note = _inline(adjudication.get("review_note"), limit=900)
        correction = _inline(adjudication.get("correction"), limit=900)
        audit_annotation = (
            f"  \n  - **수동 출처 감사**: {audit_status} "
            f"(근거 일치: {audit_match}; 재검토 필수)  \n"
            f"  - **감사 내용**: {note}  \n"
            f"  - **수정 권고**: {correction}"
            if adjudication.get("audit_id") else
            "  \n  - **수동 출처 감사**: NOT_REVIEWED"
        )
        message = (
            f"- [{_timestamp(item['timestamp'])}] "
            f"**영상에서 추출된 주장 #{n} — 사실성 미검증** "
            f"({origin}; {detail['review_priority']} / {detail['review_class']}): "
            f"{_inline(item['text'])}  \n"
            f"  - 근거 위치: {loc}  \n"
            f"  - **내용 위험 신호**: {risk_list}  \n"
            f"  - **검증 미완료 상태**: {gaps}  \n"
            f"  - **인용 의미 대응**: {detail['evidence_content_match']}"
            " (구조 검증과 의미 검증은 다름)"
            + (f"  \n  - **시간축 참고**: {contextual}" if contextual else "")
            + audit_annotation
        )
        sections[item["section_id"]].append(message)
        sections[19].append(
            f"- [{_timestamp(item['timestamp'])}] 주장 #{n}, "
            f"섹션 {item['section_id']}: {loc}; "
            f"출처 유형 {origin}; 모델 추출 인용 의미 검증 "
            f"{detail['evidence_content_match']}; "
            f"수동 감사 판정 {audit_status} / {audit_match}"
        )

    # Broad chapter summaries are NOT additional topic claims or executable
    # workflow steps. Keep them in provenance section, and use only a few
    # clearly labeled chapter outlines if no typed topic claims exist.
    chapters = analysis.get("scene_analyses", []) or []
    for sc in chapters:
        stamp = _timestamp(sc.get("start_sec", 0))
        loc = _evidence_label(sc.get("evidence_ref"),
                              sc.get("evidence_segment_ids", []))
        scenes = ", ".join(map(str, sc.get("source_scene_ids") or [])) or "미연결"
        sections[19].append(
            f"- [{stamp}] 챕터 {sc.get('chapter_id', sc.get('scene_id', '?'))}: "
            f"원본 장면 {scenes}; {loc}; "
            "챕터 요약은 모델 해석이며 독립 사실 검증 전"
        )
    if not sections[1]:
        for sc in chapters[:5]:
            if sc.get("summary"):
                stamp = _timestamp(sc.get("start_sec", 0))
                sections[1].append(
                    f"- [{stamp}] **챕터 개요 후보 (독립 검증 전):** "
                    f"{_inline(sc.get('summary'), limit=600)}"
                )

    all_prompts = analysis.get("prompts_extracted", []) or []
    for prompt in all_prompts:
        kind = str(prompt.get("evidence_kind", "SCREEN_TEXT_UNSPECIFIED")).upper()
        stamp = _timestamp(prompt.get("timestamp"))
        label = _inline(prompt.get("label"), limit=160)
        text = _inline(prompt.get("verbatim_text"))
        loc = _evidence_label(prompt.get("evidence_ref"))
        complete = str(prompt.get("transcription_completeness", "UNCONFIRMED_COMPLETE"))
        if complete == "TRUNCATED_VISIBLE":
            completeness_note = "화면에 보이는 일부만 전사 — 원문 전문 아님"
        else:
            completeness_note = "전문 여부 독립 검수 전"
        # UI placeholder and model output are NEVER counted as user prompts.
        line = (
            f"- [{stamp}] {kind} · {label}: {text}  \n"
            f"  - 근거: {loc}; 완전성: {complete} — {completeness_note}"
        )
        sections[10 if kind == "USER_PROMPT" else 19].append(line)

    settings = analysis.get("ui_settings_extracted", []) or []
    if settings:
        sections[13].append("### GUI 설정 (실제 화면에서 관찰된 후보)")
    settings_count = 0
    for setting in settings:
        name = _inline(setting.get("setting_name"), limit=500)
        # Peripheral bookmarks/profiles may reveal unrelated personal data;
        # avoid promoting them to public technical research evidence.
        if any(private in name for private in ("북마크", "프로필", "주소창", "계정명")):
            continue
        settings_count += 1
        stamp = _timestamp(setting.get("timestamp"))
        value = _inline(setting.get("selected_value"), limit=500)
        loc = _evidence_label(setting.get("evidence_ref"))
        sections[13].append(
            f"- [{stamp}] GUI · {name}: {value}  \n"
            f"  - 관찰 근거: {loc}; 원본 표시 판정: "
            f"{setting.get('status','UNVERIFIED')}"
        )
    sections[13].append("### CLI 명령어")
    cli_commands = analysis.get("cli_commands", []) or []
    if not cli_commands:
        sections[13].append(
            "실제 터미널에서 실행된 CLI 명령 0건 — "
            "GUI 조작을 CLI 실행으로 분류하지 않음."
        )
    for command in cli_commands:
        if not command.get("evidence_ref") and not command.get("source_segment_ids"):
            continue
        loc = _evidence_label(command.get("evidence_ref"),
                              command.get("source_segment_ids", []))
        sections[13].append(
            f"- [{_timestamp(command.get('timestamp'))}] CLI로 분류된 명령 "
            f"(별도 실행 검증 전): "
            f"{_inline(command.get('command'))}; 근거: {loc}"
        )

    # Workflow: a chapter summary is not a reproducible instruction and the
    # model's "one click" phrase is not evidence of a single UI operation.
    workflow_steps = analysis.get("workflow_steps", []) or []
    if workflow_steps:
        sections[14].append("### 세부 시연 단계 (모델 추출 후보)")
        for number, step in enumerate(workflow_steps, 1):
            if not step.get("evidence_ref") and not step.get("source_segment_ids"):
                continue
            origin = str(step.get("interaction_type") or "UNCONFIRMED")
            sections[14].append(
                f"{number}. [{_timestamp(step.get('timestamp'))}] "
                f"{origin} — {_inline(step.get('action'), limit=700)}  \n"
                f"   - 관찰 결과: {_inline(step.get('observed_result'), limit=700) or '미확인'}  \n"
                f"   - 근거: {_evidence_label(step.get('evidence_ref'), step.get('source_segment_ids'))}  \n"
                "   - 실행 성공 여부: 독립 검증 전"
            )
    else:
        sections[14].append(
            "**재현 가능한 단계별 Workflow 미구축:** 챕터 요약이나 '원클릭' "
            "설명만으로 실제 버튼·입력·설정·출력을 재현할 수 없습니다. "
            "아래의 연구 주장 후보를 실행 매뉴얼로 사용하지 마세요."
        )

    # Visual labels with no factual source are NOT official platform limits.
    if sections[15]:
        sections[15].insert(
            0, "**주의:** 요금제 한도·월간/사용자당 제한·모델 사용량은 "
            "영상 속 생성 슬라이드 또는 화자의 주장일 수 있습니다. "
            "공식 문서와 기준일이 검증되지 않았다면 현재 사양으로 인용하지 마세요."
        )
    if sections[9] or sections[16]:
        for number in (9, 16):
            if sections[number]:
                sections[number].insert(
                    0, "**한계:** 소스 기반 응답/인용 UI의 관찰은 "
                    "내부 RAG 아키텍처나 환각률·정확도 우위의 실증 검증이 아닙니다."
                )
    if sections[2]:
        sections[2].insert(
            0, "**모델 명칭 구분:** 화자 발화·생성 콘텐츠 표기·실제 "
            "제품 내부 구동 모델·공식 확인된 사실을 동일시하지 마세요."
        )
    if sections[17]:
        sections[17].insert(
            0, "**실패 사례 구분:** 화자의 일반 경고 및 화면에서 관찰한 "
            "현상은 직접 재현한 오류 로그와 다릅니다."
        )
    status_counts = (audited_claims or {}).get("status_counts", {})
    sections[19].insert(
        0, "**참조 상태:** PANEL/자막 ID의 구조적 유효성만 검증됩니다. "
        "모델 자체의 의미 일치 판정 EVIDENCE_CONTENT_MATCH=NOT_REVIEWED이며, "
        "선별된 주장에 한해 외부에서 수행한 수동 감사 판정을 별도로 표시합니다. "
        "감사 기록이 있더라도 모든 주장이 현재의 공식 사실로 검증되거나 "
        "보고서 게시가 승인됐다는 의미는 아닙니다. "
        + (f"수동 감사 집계: {status_counts}." if status_counts else "")
    )
    stats = {
        "typed_research_items": len(items),
        "timeline_chapters": len(chapters),
        "user_prompts": sum(
            str(p.get("evidence_kind", "")).upper() == "USER_PROMPT"
            for p in all_prompts
        ),
        "partial_user_prompts": sum(
            str(p.get("evidence_kind", "")).upper() == "USER_PROMPT" and
            str(p.get("transcription_completeness", "")) == "TRUNCATED_VISIBLE"
            for p in all_prompts
        ),
        "ui_settings": settings_count,
        "cli_commands": len(cli_commands),
        "workflow_steps": len(workflow_steps),
        "claim_review_flags": claim_flags,
        "claim_review_priority_counts": priority_counts,
        "unverified_claims": len(items),
        "independent_semantic_reviews": 0,
        "research_semantic_coverage": (
            "MODEL_REPORTED_ITEMS_PENDING_REVIEW" if items else
            "LEGACY_V3_NO_TYPED_RESEARCH_ITEMS"
        ),
    }
    return sections, stats


def build_research_report(root: Path, video_id: str, run_id: str) -> Dict[str, Any]:
    root = Path(root).resolve()
    if not VIDEO_ID.fullmatch(video_id) or not RUN_ID.fullmatch(run_id):
        raise ResearchReportBlocked("VIDEO_OR_RUN_ID_INVALID")
    run_dir = root / "data" / "videos" / video_id / "runs" / run_id
    vdir = run_dir.parent.parent

    # Re-evaluate current files, not a stale cached PASS. Never call a model.
    gate = assess_research_run(root, video_id, run_id, persist=True)
    if gate["structural_status"] != "PASS":
        raise ResearchReportBlocked("QUALITY_GATE_BLOCKED:" +
                                    ",".join(gate["errors"][:5]))

    report_path = run_dir / "research_report_19.md"
    manifest_path = run_dir / "research_report_19_manifest.json"
    with closing(open_research_db(root)) as db:
        run = db.execute(
            "SELECT status FROM analysis_runs WHERE video_id=? AND run_id=?",
            (video_id, run_id),
        ).fetchone()
        if run is None or run["status"] != "COMPLETED":
            raise ResearchReportBlocked("RUN_NOT_REGISTERED_OR_INCOMPLETE")
        old = db.execute(
            "SELECT review_state FROM reports WHERE video_id=? AND run_id=? "
            "AND report_kind='RESEARCH_19'", (video_id, run_id),
        ).fetchone()
        if old is not None and old["review_state"] in ("APPROVED", "PUBLISHED"):
            raise ResearchReportBlocked("APPROVED_REPORT_IMMUTABLE")

    analysis = read_json(run_dir / "tiered_analysis.json")
    frames = read_json(run_dir / "frames_manifest.json")
    subtitles = read_json(vdir / "parsed_subtitles.json")
    adjudications = gate["claim_adjudications"]
    sections, stats = _collect_sections(
        analysis, frames, subtitles, audited_claims=adjudications,
    )
    filled = sum(bool(sections[i]) for i in range(1, 19))
    absent = [i for i in range(1, 19) if not sections[i]]
    lines = [
        f"# YouTube 연구 보고서 — {video_id}", "",
        f"- 원본: https://www.youtube.com/watch?v={video_id}",
        f"- 실행 ID: {run_id}",
        "- 보고서: 19개 섹션 연구 초안 (REVIEW_REQUIRED)",
        f"- 기계적 근거 검사: {gate['structural_status']} (독립 사실 검증이 아님)",
        "- 주의: 영상 속 생성 콘텐츠의 주장과 원본 사실을 구별해야 합니다.",
        "- 모델의 요약·주제 분류는 사람의 별도 검수가 필요합니다.",
        "- EVIDENCE_CONTENT_MATCH=NOT_REVIEWED: 인용 번호만으로 의미적 일치는 확인되지 않습니다.",
        "- 수동 감사 결과는 특정 주장에 대한 검토 기록이며, 보고서 전체의 사실 검증이나 승인이 아닙니다.",
        f"- 수동 감사 상태: {adjudications['status_counts']} "
        f"(감사 기록: {adjudications['audit_id'] or '없음'}).",
        "", "## Executive summary", "",
        f"- 논리 챕터 {stats['timeline_chapters']}개, 화면 사용자 입력 "
        f"{stats['user_prompts']}개, UI 설정 {stats['ui_settings']}개.",
        f"- 1~18번 중 근거 후보가 있는 섹션 {filled}/18; "
        f"구조화 기술 항목 {stats['typed_research_items']}개.",
        "- 빈 섹션은 원본에서 없었다고 단정하지 않으며, "
        "분석 자료에서 미확인·미분류 상태로 처리합니다.", "",
    ]
    for number, title in enumerate(SECTION_TITLES, 1):
        lines.extend([f"## {number}. {title}", ""])
        if sections[number]:
            lines.extend(sections[number])
        else:
            lines.append(
                "현재 구조화된 원본 근거에서 직접 확인되지 않음 "
                "(미추출 가능성 포함; 추가 검수 필요)."
            )
        lines.append("")
    rendered = "\n".join(lines)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": 1, "video_id": video_id, "run_id": run_id,
        "report_kind": "RESEARCH_19", "review_state": "REVIEW_REQUIRED",
        "structural_gate": gate["structural_status"],
        "independent_factual_review_completed": False,
        "semantic_evidence_review_status": "REVIEW_REQUIRED",
        "research_claims_pending_review": len(analysis.get("research_items", []) or []),
        "claim_adjudications_audit_id": adjudications["audit_id"],
        "claim_adjudications_status_counts": adjudications["status_counts"],
        "claim_adjudications_reviewed_claims": adjudications["reviewed_claims"],
        "has_unresolved_confirmed_problem":
            adjudications["has_unresolved_confirmed_problem"],
        "claim_review_flags": stats["claim_review_flags"],
        "claim_review_priority_counts": stats["claim_review_priority_counts"],
        "unverified_claims": stats["unverified_claims"],
        "prompt_truncated_count": stats["partial_user_prompts"],
        "cli_commands_count": stats["cli_commands"],
        "workflow_steps_count": stats["workflow_steps"],
        "sections_total": 19,
        "sections_with_content_1_to_18": filled,
        "sections_without_evidence_candidates": absent,
        "evidence_stats": stats,
        "report_sha256": digest, "report_path": str(report_path),
    }
    # Keep old video and evidence outputs untouched. Rebuild only this run's
    # unapproved 19-section draft, never a human-approved report.
    atomic_write(report_path, rendered)
    atomic_write(manifest_path, json_text(manifest))
    with closing(open_research_db(root)) as db:
        with db:
            db.execute(
                """INSERT INTO reports
                (video_id,run_id,report_kind,path,review_state,updated_utc)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(video_id,run_id,report_kind)
                DO UPDATE SET path=excluded.path,updated_utc=excluded.updated_utc
                WHERE reports.review_state IN ('DRAFT','REVIEW_REQUIRED')""",
                (video_id, run_id, "RESEARCH_19", str(report_path), "REVIEW_REQUIRED"),
            )
    return manifest
