"""Command-line entrypoint: python -m ytresearch ..."""
import argparse
from contextlib import closing
from pathlib import Path
import sys

from .pipeline import analyze_one, audit_one, collect_one, export_gpt, extract_one, import_csv, list_videos
from .timeline import timeline_one, export_timeline
from .video_evidence import video_one, review_next, probe_video_access
from .batch import sync_playlist, sync_sources_csv, load_active, batch_timeline, publish_batch
from .diagnostics import list_diagnostic_summaries, inspect_saved_response_structure, inspect_saved_claim_sections, inspect_saved_document_summary, inspect_video_access_archive
from .storage import connect, update_stage_at_root

class PrerequisiteMissing(RuntimeError):
    """Missing prior-stage input is not a model/provider execution failure."""


REQUIRED_FILES = {
    "extract": ("transcript.json", "transcript.sha256"),
    "analyze": ("transcript.json", "transcript.sha256", "extraction.json"),
    "audit": (
        "transcript.json", "transcript.sha256",
        "extraction.json", "analysis.md", "analysis_manifest.json",
    ),
}


def check_prerequisites(root: Path, video_id: str, command: str) -> None:
    required = REQUIRED_FILES.get(command, ())
    base = root / "data" / "videos" / video_id
    missing = [name for name in required if not (base / name).is_file()]
    if not missing:
        return
    if any(name in missing for name in ("transcript.json", "transcript.sha256")):
        next_step = "collect"
    elif "extraction.json" in missing:
        next_step = "extract"
    else:
        next_step = "analyze"
    raise PrerequisiteMissing(
        f"required prior-stage artifacts missing ({', '.join(missing)}); "
        f"run {next_step} --video-id <registered synthetic or authorized id>"
    )



def main():
    parser = argparse.ArgumentParser(description="YouTube 타임라인 연구: 77개 활성 영상 + 이전 레지스트리 보존")
    parser.add_argument("--root", default=".", help="프로젝트 폴더 (기본값: 현재 디렉터리)")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("init")
    command.add_parser("validate-registry")
    command.add_parser("validate-active", help="현재 접근 가능한 77개 소스 목록 검증")
    rstate = command.add_parser("research-state", help="로컬 77영상 실행·보고서 상태를 안전하게 동기화 (모델 호출 없음)")
    rstate.add_argument("--expected", type=int, default=77)
    rstate.add_argument("--scan", action="store_true", help="이미 저장된 runs 체크포인트를 수집")

    csvsync = command.add_parser(
        "sync-sources",
        help="기존 sources.csv의 77개 링크를 우선 소스 원장으로 등록 (YouTube 조회 불필요)",
    )
    csvsync.add_argument("--csv", type=Path, default=Path("sources.csv"))
    csvsync.add_argument("--expected", type=int, default=77)
    sync = command.add_parser(
        "sync-playlist", help="선택적 실시간 비교: YouTube 재생목록으로 활성 영상 목록 생성"
    )
    sync.add_argument("--playlist-url", required=True)
    sync.add_argument("--expected", type=int, default=77)
    planner = command.add_parser(
        "plan-research-batch",
        help="77개 영상의 로컬 입력·기존 실행 상태를 읽고 오프라인 배치 계획만 생성",
    )
    planner.add_argument("--expected", type=int, default=77)
    planner.add_argument("--max-jobs", type=int, default=3)
    planner.add_argument("--retry-failed", action="store_true",
                         help="실패 실행 재시도 후보만 계획에 포함 (실제 재시도는 하지 않음)")
    planner.add_argument("--save", action="store_true",
                         help="무시되는 data/batch_plans/ 에 버전별 계획 JSON 저장")
    checkpoint = command.add_parser(
        "batch-checkpoint",
        help="Phase 5D: 9단계 합성 체크포인트 검사/기록 (AGY 호출 없음)",
    )
    checkpoint.add_argument("operation", choices=("status", "record"))
    checkpoint.add_argument("--plan-sha", required=True)
    checkpoint.add_argument("--video-id", required=True)
    checkpoint.add_argument("--lease-token")
    checkpoint.add_argument("--stage")
    checkpoint.add_argument("--input-sha256")
    checkpoint.add_argument("--output-sha256")
    stress = command.add_parser(
        "phase5d-stress",
        help="임시 합성 원장으로 다중 작업자/429/체크포인트 스트레스 검증",
    )
    stress.add_argument("--workers", type=int, default=12)
    stress.add_argument("--rounds", type=int, default=3)
    p5e = command.add_parser(
        "phase5e-validate",
        help="Phase 5E: 통합 전 작업자 안전 계약·무결성·사용량 오프라인 검증 (외부 호출 0건)",
    )
    command.add_parser(
        "phase5f-validate",
        help="Phase 5F: 모든 입력을 임시 합성 공간에 생성해 복구·429·무결성 검증 (외부 호출 0건)",
    )
    bq = command.add_parser(
        "batch-queue",
        help="Phase 5B: 모델 호출 없는 단일 작업자 임대·복구 모의 실행",
    )
    bq.add_argument("operation", choices=("init","status","claim","finish","recover","retry"))
    bq.add_argument("--plan-sha", required=True)
    bq.add_argument("--video-id")
    bq.add_argument("--owner")
    bq.add_argument("--lease-token")
    bq.add_argument("--failure", action="store_true")
    bq.add_argument("--reason")
    bq.add_argument("--model", help="예산 정책에 등록된 정확한 AGY 모델명 (dry-run만)")
    bq.add_argument("--usage-file", type=Path, help="합성 입력·캐시·출력 토큰 JSON")
    bq.add_argument("--rate-error-code", choices=(
        "429", "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED"
    ), help="모의 요청의 Rate Limit 오류를 전역 차단에 즉시 기록")
    bq.add_argument("--retry-after-seconds", type=int, default=0)
    budget = command.add_parser(
        "batch-budget",
        help="Phase 5C: 모델별 보수적 예산/전역 Rate Limit 제어 (모델 호출 없음)",
    )
    budget.add_argument("operation", choices=("configure","status","pause","resume","reconcile"))
    budget.add_argument("--plan-sha", required=True)
    budget.add_argument("--policy-file", type=Path)
    budget.add_argument("--usage-file", type=Path)
    budget.add_argument("--video-id")
    budget.add_argument("--actor")
    budget.add_argument("--reason")
    budget.add_argument("--error-code", choices=(
        "429", "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED"
    ))
    budget.add_argument("--retry-after-seconds", type=int, default=0)
    batch = command.add_parser("batch-timeline", help="활성 영상만 순차 수집·agy 기록·내보내기")
    batch.add_argument("--expected", type=int, default=77)
    batch.add_argument("--max-videos", type=int, default=3)
    batch.add_argument(
        "--source-mode",
        choices=("transcript-first", "agy-only", "gemini-only"),
        default="transcript-first",
        help="자막 우선 / AGY 영상 직접 조사만 / Gemini API 동영상 URI만",
    )
    batch.add_argument("--retry-blocked", action="store_true",
                       help="이전 실패로 격리된 영상도 다시 시도")
    probe = command.add_parser(
        "probe-video", help="AGY URL 읽기 도구의 실제 호출 여부만 최소 비용으로 검사"
    )
    probe.add_argument("--video-id", required=True)
    vid = command.add_parser(
        "agy-video",
        help="자막 없이 AGY 도구로 영상 조사하거나 기존 자막 청크에 영상 근거 보완",
    )
    vid.add_argument("--video-id", required=True)
    vid.add_argument("--mode", choices=("full", "review"), default="full")
    vid.add_argument("--provider", choices=("agy", "gemini-url"), default="agy")
    vid.add_argument("--chunk", type=int, default=None,
                     help="review 모드에서 특정 청크 하나만 확인")
    vid.add_argument("--max-chunks", type=int, default=1,
                     help="review 모드에서 기존 기록과 대조할 청크 수")
    pub = command.add_parser("publish-batch", help="Public 배포본에서 비활성화된 연구자료 Git 게시 명령")
    pub.add_argument("--expected", type=int, default=77)
    pub.add_argument("--commit", action="store_true")
    pub.add_argument("--push", action="store_true")
    pub.add_argument(
        "--allow-whitespace-warnings", action="store_true",
        help="Git 공백 경고만 명시적으로 허용; 원본 자막과 AGY 기록은 수정하지 않음",
    )
    pub.add_argument("--acknowledge-rights", action="store_true",
                     help="자막 원문 재게시 권한 및 공개 적절성을 확인함")
    diag = command.add_parser("diagnostics", help="로컬 agy 원본 기록의 안전한 메타데이터만 확인")
    diag.add_argument("--video-id", default=None)
    diag.add_argument("--limit", type=int, default=20)
    diag.add_argument("--inspect-sections", action="store_true",
                      help="최신 원본의 claim.section 자료형·숫자 분포만 출력; AI 재호출 없음")
    diag.add_argument("--inspect-summary", action="store_true",
                      help="최신 agy 원본의 복수 JSON 문서/배열/문자열 분포를 값 없이 요약")
    diag.add_argument("--inspect-video-access", action="store_true",
                      help="저장된 AGY video_source 호출 도구·권한 상태만 출력; URL·모델 본문·토큰·원문 미표시")
    diag.add_argument("--inspect-keys", action="store_true",
                      help="최신 agy 원본의 JSON 필드 이름·자료형만 로컬 출력 (본문은 출력하지 않음)")
    diag.add_argument("--export-safe", type=Path, default=None,
                      help="원문 없는 진단 요약 목록을 JSON으로 출력 (파일 내용 검토 후 공유)")
    imp = command.add_parser("import")
    imp.add_argument("csv", type=Path)
    vtt = command.add_parser("import-vtt")
    vtt.add_argument("--video-id", required=True)
    vtt.add_argument("--file", type=Path, required=True)
    vtt.add_argument("--language", default="und")
    vtt.add_argument("--expected", type=int, default=77,
                     help="활성 CSV 영상 목록의 소스 엔트리 수 (기본 77)")
    j3 = command.add_parser("import-json3", help="YouTube JSON3 자막 파일을 코드 기반으로 파싱·저장")
    j3.add_argument("--video-id", required=True)
    j3.add_argument("--file", type=Path, required=True)
    j3.add_argument("--language", default="ko")
    j3.add_argument("--expected", type=int, default=77)
    risk_review = command.add_parser(
        "review-research-claims",
        help="기존 분석 JSON의 P0/P1/P2 주장 위험도를 오프라인 재평가 (AGY 호출 없음)",
    )
    risk_review.add_argument("--video-id", required=True)
    risk_review.add_argument("--run-id", required=True)
    audit_import = command.add_parser(
        "import-claim-audit",
        help="검수된 주장 판정을 원본 해시 검증 후 별도 파일로 가져오기 (AGY 미사용)",
    )
    audit_import.add_argument("--file", required=True, type=Path)
    remstat = command.add_parser(
        "claim-remediation-status", help="감사 확정 오류·충돌 수정 상태 확인 (오프라인)"
    )
    remstat.add_argument("--video-id", required=True)
    remstat.add_argument("--run-id", required=True)
    remedit = command.add_parser(
        "record-claim-remediation",
        help="감사 오류의 CORRECTED→RECHECKED→RESOLVED 수동 전이 (AGY 사용 안 함)",
    )
    remedit.add_argument("--video-id", required=True)
    remedit.add_argument("--run-id", required=True)
    remedit.add_argument("--item-index", type=int, required=True)
    remedit.add_argument("--next-status", choices=("CORRECTED","RECHECKED","RESOLVED"), required=True)
    remedit.add_argument("--actor", required=True)
    remedit.add_argument("--reason", required=True)
    remedit.add_argument("--correction-file")
    report19 = command.add_parser("build-research-report", help="검증 PASS 실행에서 19섹션 비승인 연구 초안 생성")
    report19.add_argument("--video-id", required=True)
    report19.add_argument("--run-id", required=True)
    gate = command.add_parser("validate-research-run", help="오프라인 분석 증거 품질 검사; 통과해도 사람 승인 필요")
    gate.add_argument("--video-id", required=True)
    gate.add_argument("--run-id", required=True)
    research = command.add_parser(
        "run-research-video", help="등록된 영상 1개의 원본→멀티모달 분석→상태 등록 (실 AGY 명시 승인 필요)",
    )
    research.add_argument("--video-id", required=True)
    research.add_argument("--video", type=Path, required=True)
    research.add_argument("--subtitle-json3", type=Path, required=True)
    research.add_argument("--highres-video", type=Path)
    research.add_argument("--audio", type=Path)
    research.add_argument("--primary-model", choices=("argon","flash","auto"), default="auto")
    research.add_argument("--run-id")
    research.add_argument("--max-keyframes", type=int, default=32)
    research.add_argument("--roi-mode", choices=("auto","off"), default="auto")
    research.add_argument("--live-agy", action="store_true", help="실 AGY 모델 호출·할당량 사용 명시적으로 허용")
    research.add_argument("--use-argon-verifier", action="store_true")
    research.add_argument("--force", action="store_true")
    research.add_argument("--expected", type=int, default=77)
    mm = command.add_parser(
        "multimodal-run",
        help="코드 기반 JSON3·고해상도 키프레임 전처리 및 Flash/Argon 체크포인트 멀티모달 파이프라인 실행",
    )
    mm.add_argument("--video-id", required=True)
    mm.add_argument("--video", "--video-file", dest="video", type=Path, required=True, help="기본 영상 파일 경로 (예: 720p MP4)")
    mm.add_argument("--subtitle-json3", "--subtitle-file", dest="subtitle_json3", type=Path, required=True, help="원본 JSON3 자막 파일 경로")
    mm.add_argument("--audio", "--audio-file", dest="audio", type=Path, default=None, help="선택적 오디오 파일 경로 (M4A/WAV)")
    mm.add_argument("--highres-video", "--highres-video-file", dest="highres_video", type=Path, default=None, help="선택적 고해상도(4K/1080p) 영상 파일 경로")
    mm.add_argument(
        "--primary-model",
        choices=("argon", "flash", "auto"),
        default="auto",
        help="1차 분석 모델 선택: argon(gemini-4-argon-high 독립 1차 분석), flash(gemini-3.8-flash-medium 독립 1차 분석), auto(적응형 모델 선택 정책)",
    )
    mm.add_argument("--run-id", default=None, help="실행 구성별 독립 체크포인트·산출물 식별자 (미지정 시 모델·프롬프트·보드 구성 해시로 자동 생성)")
    mm.add_argument("--panels-per-board", type=int, default=2, help="Evidence Board 당 결합할 고해상도 패널 수 (기본값: 2)")
    mm.add_argument("--max-keyframes", type=int, default=16, help="추출할 최대 고해상도 UI 키프레임 수 (기본값: 16)")
    mm.add_argument("--roi-mode", choices=("auto", "off"), default="auto", help="전체 4K 프레임은 항상 보존하고 좌·중·우 UI 확대 ROI를 추가 추출 (기본 auto)")
    mm.add_argument("--title", default=None, help="보고서 제목")
    mm.add_argument("--url", default=None, help="원본 YouTube URL")
    mm.add_argument("--use-agy-flash", action="store_true", help="실제 AGY CLI Gemini 3.8 Flash 멀티모달 분석 실행")
    mm.add_argument("--use-agy-argon", action="store_true", help="불확실 구간에 대해 AGY CLI Gemini 4 Argon 조건부 검증 허용")
    mm.add_argument("--force", action="store_true", help="기존 체크포인트를 무시하고 처음부터 재실행")
    for name in ("collect", "extract", "timeline", "export-timeline", "analyze", "audit", "run", "status", "export-gpt"):
        cmd = command.add_parser(name)
        cmd.add_argument("--video-id", default=None)
        if name == "extract":
            cmd.add_argument("--archives-only", action="store_true",
                             help="저장된 agy 원본만 복구; 새로운 모델 호출 없이 청크 추출")
            cmd.add_argument("--quarantine-mixed", action="store_true",
                             help="저장된 혼합 JSON의 첫 분석 문서를 조건부 추출; "
                                  "보조 문서는 검수 대기로 보존 (archives-only 필수)")
        if name != "export-gpt":
            cmd.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    # Public releases may analyze synthetic fixtures but must never Git-publish
    # private corpora. Reject before reading --root, configuration or registry.
    if args.command == "publish-batch":
        parser.error("PUBLIC_REPOSITORY_RESEARCH_PUBLISH_DISABLED")
    if args.command == "phase5f-validate":
        # Runs in disposable isolated temporary workspaces only. The provided
        # --root is deliberately ignored: no private registry or provider calls.
        import json
        from .phase5f_validation import run_phase5f_validation_suite
        report = run_phase5f_validation_suite()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failed_scenarios"] == 0 else 2
    root = Path(args.root).resolve()
    if (args.command == "extract" and getattr(args, "quarantine_mixed", False)
            and not args.archives_only):
        parser.error("--quarantine-mixed는 --archives-only와 함께 사용해야 합니다.")
    if not (root / "config" / "agents.json").exists():
        parser.error(f"config/agents.json을 찾을 수 없습니다: {root}")
    if args.command == "review-research-claims":
        import json
        from .claim_review import review_cached_research_claims
        result = review_cached_research_claims(root, args.video_id, args.run_id)
        print(json.dumps({
            "video_id": result["video_id"],
            "run_id": result["run_id"],
            "claims_total": result["claims_total"],
            "priority_counts": result["priority_counts"],
            "unverified_claims": result["unverified_claims"],
            "output_path": result["output_path"],
            "independent_semantic_checks_completed":
                result["independent_semantic_checks_completed"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "import-claim-audit":
        import json
        from .claim_adjudication import import_claim_audit
        info = import_claim_audit(root, args.file)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if args.command == "claim-remediation-status":
        import json
        from .claim_remediation import summarize_claim_remediations
        print(json.dumps(
            summarize_claim_remediations(root, args.video_id, args.run_id),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if args.command == "record-claim-remediation":
        import json
        from .claim_remediation import record_claim_remediation
        print(json.dumps(record_claim_remediation(
            root, args.video_id, args.run_id,
            item_index=args.item_index, next_status=args.next_status,
            actor=args.actor, reason=args.reason,
            correction_file=args.correction_file,
        ), ensure_ascii=False, indent=2))
        return 0
    if args.command == "build-research-report":
        import json
        from .research_report import build_research_report
        result = build_research_report(root, args.video_id, args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-research-run":
        import json
        from .quality_gate import assess_research_run
        report = assess_research_run(root, args.video_id, args.run_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["structural_status"] == "PASS" else 2
    if args.command == "run-research-video":
        import json
        from .research_runner import run_research_video
        output = run_research_video(
            root, args.video_id,
            video_file=args.video,
            subtitle_json3=args.subtitle_json3,
            highres_video=args.highres_video,
            audio_file=args.audio,
            primary_model=args.primary_model,
            run_id=args.run_id,
            max_keyframes=args.max_keyframes,
            roi_mode=args.roi_mode,
            use_live_agy=args.live_agy,
            use_argon_verification=args.use_argon_verifier,
            force=args.force,
            expected_sources=args.expected,
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0 if output["quality_status"] == "PASS" else 2
    if args.command == "phase5d-stress":
        import json
        from .batch_stress import run_phase5d_synthetic_stress
        result = run_phase5d_synthetic_stress(
            workers=args.workers, rounds=args.rounds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "phase5e-validate":
        import json
        from .batch_worker_contract import run_phase5e_contract_validation_suite
        result = run_phase5e_contract_validation_suite()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["failed_scenarios"] == 0 else 2
    if args.command == "batch-checkpoint":
        import json
        from .batch_checkpoint import (
            synthetic_stage_status, record_synthetic_stage,
        )
        if args.operation == "status":
            result = synthetic_stage_status(
                root, args.plan_sha, args.video_id,
            )
        else:
            if not all((
                args.lease_token, args.stage,
                args.input_sha256, args.output_sha256,
            )):
                parser.error(
                    "batch-checkpoint record에는 lease-token, stage, input-sha256, output-sha256가 필요합니다."
                )
            result = record_synthetic_stage(
                root, args.plan_sha, args.video_id,
                lease_token=args.lease_token,
                stage_name=args.stage,
                input_sha256=args.input_sha256,
                output_sha256=args.output_sha256,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "batch-budget":
        import json
        from .storage import read_json
        from .batch_budget import (
            configure_batch_budget, batch_budget_status,
            mark_global_rate_limited, resume_global_rate_limit,
            reconcile_paused_reservation,
        )
        if args.operation == "configure":
            if args.policy_file is None:
                parser.error("batch-budget configure에는 --policy-file이 필요합니다.")
            result = configure_batch_budget(root, args.plan_sha, args.policy_file)
        elif args.operation == "status":
            result = batch_budget_status(root, args.plan_sha)
        elif args.operation == "pause":
            if not args.error_code or not args.actor or not args.reason:
                parser.error("batch-budget pause에는 --error-code, --actor, --reason이 필요합니다.")
            result = mark_global_rate_limited(
                root, args.plan_sha, error_code=args.error_code,
                actor=args.actor, reason=args.reason,
                retry_after_seconds=args.retry_after_seconds,
            )
        elif args.operation == "resume":
            if not args.actor or not args.reason:
                parser.error("batch-budget resume에는 --actor, --reason이 필요합니다.")
            result = resume_global_rate_limit(
                root, args.plan_sha, actor=args.actor, reason=args.reason,
            )
        else:
            if not args.actor or not args.reason or not args.video_id or not args.usage_file:
                parser.error("batch-budget reconcile에는 --video-id, --actor, --reason, --usage-file이 필요합니다.")
            result = reconcile_paused_reservation(
                root, args.plan_sha, args.video_id,
                actor=args.actor, reason=args.reason,
                usage=read_json(args.usage_file),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "batch-queue":
        import json
        from .batch_queue import (
            initialize_batch_queue, batch_queue_status,
            claim_batch_job, finish_batch_job, recover_batch_job,
        )
        from .storage import read_json
        if args.operation == "init":
            data = initialize_batch_queue(root, args.plan_sha)
        elif args.operation == "status":
            data = batch_queue_status(root, args.plan_sha)
        elif args.operation == "claim":
            if not args.owner:
                parser.error("batch-queue claim은 --owner가 필요합니다.")
            data = claim_batch_job(root, args.plan_sha, owner=args.owner, model=args.model)
        elif args.operation == "finish":
            if not args.video_id or not args.lease_token:
                parser.error("batch-queue finish에는 --video-id와 --lease-token이 필요합니다.")
            data = finish_batch_job(
                root, args.plan_sha, video_id=args.video_id,
                lease_token=args.lease_token, success=not args.failure,
                usage=read_json(args.usage_file) if args.usage_file else None,
                rate_error_code=args.rate_error_code,
                retry_after_seconds=args.retry_after_seconds,
            )
        else:
            if not args.video_id or not args.owner or not args.reason:
                parser.error("batch-queue recover/retry에는 --video-id, --owner, --reason이 필요합니다.")
            data = recover_batch_job(
                root, args.plan_sha, video_id=args.video_id,
                actor=args.owner, reason=args.reason,
                retry=args.operation == "retry",
            )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.command == "plan-research-batch":
        import json
        from .batch_plan import plan_research_batch
        data = plan_research_batch(
            root, expected=args.expected, max_jobs=args.max_jobs,
            retry_failed=args.retry_failed, save=args.save,
        )
        print(json.dumps({
            "execution_mode": data["execution_mode"],
            "live_gate": data["live_gate"],
            "source_entries": data["source_entries"],
            "unique_videos": data["unique_videos"],
            "disposition_counts": data["disposition_counts"],
            "plan_only_video_ids": data["plan_only_video_ids"],
            "plan_sha256": data["plan_sha256"],
            "saved_plan_path": data.get("saved_plan_path"),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "research-state":
        import json
        from .research_state import (
            sync_research_sources, scan_local_runs, snapshot_research_state,
        )
        counts = sync_research_sources(root, expected=args.expected)
        runs = scan_local_runs(root) if args.scan else {"imported_runs": [], "errors": []}
        snap = snapshot_research_state(root)
        print(json.dumps({"registration": counts, "run_scan": runs,
                          "summary": snap["summary"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sync-sources":
        result = sync_sources_csv(root, args.csv, expected=args.expected)
        print(
            f"CSV 원장 등록 완료: 링크={result['source_entries_count']}, "
            f"고유 영상={result['unique_video_count']}, "
            f"중복 링크={result['duplicate_source_entries']}, "
            f"출처={result['source_file']}"
        )
        return 0
    if args.command == "sync-playlist":
        result = sync_playlist(root, args.playlist_url, expected=args.expected)
        print(f"활성 영상 {result['accessible_count']}/{args.expected}; "
              f"삭제/비공개/중복 위치 {len(result['unavailable_positions'])}; "
              f"원본 저장 data/active_playlist.json")
        return 0
    if args.command == "validate-active":
        active = load_active(root)
        if active.get("source_origin") == "sources_csv":
            print(
                f"CSV 소스 링크: {active['source_entries_count']}/77; "
                f"고유 영상: {active['unique_video_count']}; "
                "실제 YouTube 접근 가능 여부는 개별 수집 시 확인"
            )
        else:
            print(f"재생목록 영상: {active['accessible_count']}/77")
            print(f"제외된 위치: {active.get('unavailable_positions', [])}")
        return 0
    if args.command == "batch-timeline":
        result = batch_timeline(
            root, expected=args.expected, max_videos=args.max_videos,
            retry_blocked=args.retry_blocked,
            source_mode=args.source_mode,
        )
        # An intentionally partial batch is a successful checkpoint, not a CLI
        # failure. Inspect research/index.json for blocked/pending sources.
        return 0
    if args.command == "probe-video":
        import json
        active = load_active(root, expected=77)
        if args.video_id not in {item["video_id"] for item in active["sources"]}:
            parser.error("probe-video 대상이 sources.csv 활성 원장에 없습니다")
        summary = probe_video_access(root, args.video_id)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "agy-video":
        active = load_active(root, expected=77)
        if args.video_id not in {row["video_id"] for row in active["sources"]}:
            parser.error("agy-video 대상이 sources.csv 활성 원장에 없습니다")
        if args.mode == "review":
            if args.chunk is not None:
                result = video_one(
                    root, args.video_id, mode="review", chunk=args.chunk,
                    provider=args.provider,
                )
                print(f"{args.video_id}: reviewed chunk {args.chunk}, "
                      f"access={result['access_status']}")
            else:
                completed = review_next(
                    root, args.video_id, provider=args.provider,
                    max_chunks=args.max_chunks,
                )
                print(f"{args.video_id}: reviewed chunks {completed}")
        else:
            if args.chunk is not None:
                parser.error("full 모드는 --chunk 사용 불가")
            result = video_one(
                root, args.video_id, mode="full", provider=args.provider,
            )
            print(f"{args.video_id}: video-only evidence saved; "
                  f"access={result['access_status']}, review required")
        return 0
    if args.command == "init":
        db = connect(root)
        db.close()
        print(f"Initialized: {root / 'data' / 'state.sqlite3'}")
    elif args.command == "import":
        print(f"새 엔트리: {import_csv(root, args.csv.resolve())}")
    elif args.command == "import-vtt":
        from .collector import ingest_vtt
        # CSV-first projects may not have copied their source entries to the
        # legacy SQLite registry. Check the verified local active CSV ledger
        # without modifying or reassigning any existing S-number.
        try:
            list_videos(root, only_video=args.video_id)
        except ValueError:
            active = load_active(root, expected=args.expected)
            if args.video_id not in {x["video_id"] for x in active["sources"]}:
                raise ValueError(
                    "IMPORT_VTT_UNREGISTERED_VIDEO: 활성 원장에도 없는 Video ID"
                )
        sha, count = ingest_vtt(root, args.video_id, args.file.resolve(), args.language)
        update_stage_at_root(root, args.video_id, "COLLECTED", transcript_sha=sha)
        print(f"Imported VTT {args.video_id}: {count} cues")
    elif args.command == "import-json3":
        from .collector import ingest_json3
        try:
            list_videos(root, only_video=args.video_id)
        except ValueError:
            active = load_active(root, expected=args.expected)
            if args.video_id not in {x["video_id"] for x in active["sources"]}:
                raise ValueError(
                    "IMPORT_JSON3_UNREGISTERED_VIDEO: 활성 원장에도 없는 Video ID"
                )
        sha, count = ingest_json3(root, args.video_id, args.file.resolve(), args.language)
        update_stage_at_root(root, args.video_id, "COLLECTED", transcript_sha=sha)
        print(f"Imported JSON3 {args.video_id}: {count} segments")
    elif args.command == "multimodal-run":
        import json
        from .multimodal import run_resumable_multimodal_pipeline
        summary = run_resumable_multimodal_pipeline(
            root,
            args.video_id,
            video_path=args.video.resolve(),
            subtitle_json3_path=args.subtitle_json3.resolve(),
            audio_path=args.audio.resolve() if args.audio else None,
            highres_video_path=args.highres_video.resolve() if args.highres_video else None,
            title=args.title,
            url=args.url,
            primary_model=args.primary_model,
            use_agy_flash=args.use_agy_flash,
            use_agy_argon=args.use_agy_argon,
            panels_per_board=args.panels_per_board,
            max_keyframes=args.max_keyframes,
            roi_mode=args.roi_mode,
            run_id=args.run_id,
            copy_files=True,
            force=args.force,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "diagnostics":
        import json
        if args.inspect_video_access:
            if not args.video_id:
                parser.error("--inspect-video-access에는 --video-id가 필요합니다")
            print(json.dumps(
                inspect_video_access_archive(root, args.video_id),
                ensure_ascii=False, indent=2,
            ))
            return 0
        entries = list_diagnostic_summaries(root, video_id=args.video_id, limit=args.limit)
        for file_path, item in entries:
            print(
                f"{item.get('created_utc', '?')} "
                f"video={item.get('video_id', '?')} "
                f"unit={item.get('unit', '?')} "
                f"outcome={item.get('outcome', '?')} "
                f"error={item.get('error_code') or '-'}"
            )
            print(f"  LOCAL ONLY: {file_path.parent}")
        print(f"Diagnostics found: {len(entries)}")
        if args.inspect_summary:
            if not entries:
                print("해당 영상의 저장된 진단 결과가 없습니다.")
            else:
                print("LATEST AGY DOCUMENT FORMAT SUMMARY (no content):")
                print(json.dumps(
                    inspect_saved_document_summary(entries[0][0].parent),
                    ensure_ascii=False, indent=2,
                ))
        if args.inspect_keys:
            if not entries:
                print("해당 영상의 진단 원본이 아직 없습니다.")
            else:
                print("LATEST ARCHIVE STRUCTURE (local inspection only):")
                print(json.dumps(
                    inspect_saved_response_structure(entries[0][0].parent),
                    ensure_ascii=False, indent=2,
                ))
        if args.inspect_sections:
            if not entries:
                print("해당 영상의 저장된 진단 결과가 없습니다.")
            else:
                print("LATEST CLAIM SECTIONS (local inspection only):")
                print(json.dumps(
                    inspect_saved_claim_sections(entries[0][0].parent),
                    ensure_ascii=False, indent=2,
                ))
        if args.export_safe:
            output = args.export_safe.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            safe = [item for _, item in entries]
            with output.open("x", encoding="utf-8") as target:
                json.dump(safe, target, ensure_ascii=False, indent=2)
                target.write("\n")
            print(f"SAFE SUMMARY exported: {output}")
    elif args.command == "validate-registry":
        with closing(connect(root)) as db:
            entries = db.execute("SELECT source_id, video_id FROM entries ORDER BY source_id").fetchall()
        expected = {f"S{i:03d}" for i in range(1, 82)}
        found = {x["source_id"] for x in entries}
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        distinct = len({x["video_id"] for x in entries})
        print(f"등록 Source Entry: {len(entries)}/81")
        print(f"고유 Video ID: {distinct}; 중복 Entry: {len(entries) - distinct}")
        print(f"누락 ID: {missing}")
        print(f"예상 외 ID: {extra}")
        return 0 if not missing and not extra and len(entries) == 81 else 2
    else:
        videos = list_videos(root, args.video_id, getattr(args, "limit", None))
        if args.command == "status":
            with closing(connect(root)) as db:
                for row in videos:
                    ids = [x["source_id"] for x in db.execute("SELECT source_id FROM entries WHERE video_id=?", (row["video_id"],)).fetchall()]
                    print(f"{','.join(ids):18} {row['video_id']}  {row['stage']}" + (f"  {row['last_error']}" if row['last_error'] else ""))
            print(f"Unique videos: {len(videos)}")
        elif args.command == "export-gpt":
            for row in videos:
                print(export_gpt(root, row["video_id"]))
        else:
            failures = 0
            for row in videos:
                vid = row["video_id"]
                try:
                    check_prerequisites(root, vid, args.command)
                    if args.command in ("collect", "run"):
                        if not (root / "data" / "videos" / vid / "transcript.json").exists():
                            print(collect_one(root, vid))
                    if args.command in ("extract", "run"):
                        print(vid, "extracted", extract_one(
                            root, vid,
                            archives_only=getattr(args, "archives_only", False),
                            quarantine_mixed=getattr(args, "quarantine_mixed", False),
                        ))
                    if args.command == "timeline":
                        print(vid, "timeline_chunks", timeline_one(root, vid))
                    if args.command == "export-timeline":
                        print(vid, "research_export", export_timeline(root, vid))
                    if args.command in ("analyze", "run"):
                        print(vid, "analyzed", analyze_one(root, vid))
                    if args.command in ("audit", "run"):
                        print(vid, "audited", audit_one(root, vid))
                except PrerequisiteMissing as exc:
                    # No provider operation occurred. Preserve the existing stage.
                    failures += 1
                    print(f"PREREQUISITE_MISSING {vid}: {exc}", file=sys.stderr)
                except Exception as exc:
                    failures += 1
                    update_stage_at_root(root, vid, f"BLOCKED_{args.command.upper()}", error=str(exc)[:500])
                    print(f"BLOCKED {vid}: {exc}", file=sys.stderr)
            if failures:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
