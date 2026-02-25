"""
web/app.py — TenAsia Intelligence Hub Streamlit UI

실행 방법:
  streamlit run web/app.py --server.port 8501

내부 API (web/api.py) 를 통해 작업 큐 및 EC2 스크래퍼를 제어합니다.
  - DB Queue 방식: 작업을 큐에 추가 → EC2 워커가 폴링하여 처리
  - SSM SendCommand 방식: EC2 에 즉시 명령 전송
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import requests
import streamlit as st

# ── 설정 ─────────────────────────────────────────────────────
API_BASE = "http://localhost:8000"
LOG_FILE  = "logs/app.log"
LOG_TAIL  = 10  # 로그 뷰어에 표시할 줄 수

PLATFORM_OPTIONS = ["x", "instagram", "facebook", "threads", "naver_blog"]
LANGUAGE_OPTIONS = {"한국어": "kr", "English": "en", "日本語": "jp"}

st.set_page_config(
    page_title="TenAsia Intelligence Hub",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── API 헬퍼 ─────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> dict[str, Any] | list | None:
    """내부 FastAPI 호출. 오류 시 st.error 표시 후 None 반환."""
    url = f"{API_BASE}{path}"
    try:
        resp = requests.request(method, url, timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        st.error("⚠️ 내부 API 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.")
    except requests.exceptions.HTTPError as exc:
        st.error(f"API 오류 {exc.response.status_code}: {exc.response.text}")
    except Exception as exc:
        st.error(f"예기치 않은 오류: {exc}")
    return None


def _read_log_tail(n: int = LOG_TAIL) -> list[str]:
    """로그 파일의 마지막 n줄을 반환합니다. 파일이 없으면 빈 리스트."""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip() for ln in lines[-n:]]
    except FileNotFoundError:
        return []
    except Exception as exc:
        return [f"[로그 읽기 오류] {exc}"]


# ── 사이드바 ──────────────────────────────────────────────────

with st.sidebar:
    st.title("📡 TenAsia Hub")
    st.caption("Intelligence Scraper Control Panel")

    st.divider()

    # 큐 상태 요약
    stats = _api("GET", "/jobs/stats")
    if stats:
        col1, col2 = st.columns(2)
        col1.metric("대기",   stats.get("pending",   0))
        col2.metric("실행 중", stats.get("running",   0))
        col1.metric("완료",   stats.get("completed", 0))
        col2.metric("실패",   stats.get("failed",    0))

    st.divider()

    # [KO / EN] 언어 토글 — 기사 뷰어의 표시 언어를 제어합니다
    st.caption("기사 표시 언어")
    st.radio(
        "표시 언어",
        ["KO", "EN"],
        horizontal=True,
        key="lang_display",
        label_visibility="collapsed",
    )

    st.divider()
    if st.button("🔄 새로고침", use_container_width=True):
        st.rerun()


# ── 탭 레이아웃 ───────────────────────────────────────────────

tab_dashboard, tab_scrape, tab_queue, tab_ssm, tab_history, tab_articles, tab_glossary, tab_artists = st.tabs(
    [
        "🏠 대시보드",
        "🕷️ 스크래핑 제어",
        "📥 작업 큐 (비동기)",
        "⚡ 즉시 실행 (SSM)",
        "📋 작업 히스토리",
        "📝 기사 뷰어",
        "📚 Glossary",
        "🎤 아티스트 관리",
    ]
)


# ─────────────────────────────────────────────────────────────
# TAB 1: 대시보드
# ─────────────────────────────────────────────────────────────

with tab_dashboard:
    st.header("시스템 현황 대시보드")

    status = _api("GET", "/status")

    if status:
        db      = status.get("db", {})
        arts    = db.get("articles", {})
        artists = db.get("artists", {})
        queue   = status.get("queue", {})
        tasks   = status.get("scrape_tasks", {})

        # ── 핵심 지표 ──
        st.subheader("핵심 지표")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("아티스트 총합",        artists.get("total",    0))
        c2.metric("오늘 수집된 기사",      arts.get("today",       0))
        c3.metric("MANUAL_REVIEW 기사",   arts.get("manual_review", 0))
        c4.metric("전체 기사 수",          arts.get("total",       0))

        # ── AI 처리 현황 ──
        st.subheader("AI 처리 현황")
        status_order = ["pending", "processing", "completed", "manual_review", "failed", "skipped"]
        cols = st.columns(len(status_order))
        for col, key in zip(cols, status_order):
            col.metric(key.upper(), arts.get(key, 0))

        st.divider()

        # ── 작업 큐 현황 ──
        col_q, col_t = st.columns(2)
        with col_q:
            st.subheader("작업 큐")
            q_c1, q_c2 = st.columns(2)
            q_c1.metric("대기",    queue.get("pending",   0))
            q_c2.metric("실행 중", queue.get("running",   0))
            q_c1.metric("완료",    queue.get("completed", 0))
            q_c2.metric("실패",    queue.get("failed",    0))

        with col_t:
            st.subheader("스크래핑 태스크")
            running = tasks.get("running", [])
            st.metric("실행 중 태스크", len(running))
            if running:
                for t in running:
                    req = t.get("request", {})
                    st.caption(
                        f"task_id: `{t['task_id'][:8]}…` | "
                        f"{req.get('start_date')} ~ {req.get('end_date')} | "
                        f"lang={req.get('language')}"
                    )

    st.divider()

    # ── 로그 뷰어 ──
    st.subheader("실시간 로그 (최근 10줄)")
    log_lines = _read_log_tail(LOG_TAIL)
    if log_lines:
        st.code("\n".join(log_lines), language="text")
    else:
        st.info(f"`{LOG_FILE}` 파일이 없거나 비어 있습니다.")

    if st.button("🔄 로그 새로고침", key="refresh_log"):
        st.rerun()

    st.divider()

    # ── 비용 리포트 ────────────────────────────────────────────
    st.subheader("💰 오늘의 비용 리포트")
    st.caption("Gemini 2.0 Flash 기준 • 입력 $0.075/1M · 출력 $0.300/1M")

    cost = _api("GET", "/reports/cost/today")

    if cost:
        usage   = cost.get("usage",   {})
        cost_d  = cost.get("cost",    {})
        savings = cost.get("savings", {})

        # ── 사용량 지표 ──
        u1, u2, u3, u4 = st.columns(4)
        u1.metric("API 호출 수",      f"{usage.get('api_calls', 0):,}")
        u2.metric("총 토큰",           f"{usage.get('total_tokens', 0):,}")
        u3.metric("입력 토큰",         f"{usage.get('prompt_tokens', 0):,}")
        u4.metric("출력 토큰",         f"{usage.get('completion_tokens', 0):,}")

        # ── 비용 & 절감 지표 ──
        c1, c2, c3, c4 = st.columns(4)
        actual  = cost_d.get("actual_total_usd",         0.0)
        saved   = savings.get("saved_cost_usd_est",      0.0)
        total_if = savings.get("total_if_no_priority_usd", 0.0)
        rate    = round(saved / total_if * 100, 1) if total_if > 0 else 0.0

        c1.metric("실제 비용 (오늘)",      f"${actual:.4f}")
        c2.metric("Priority 절감 추정액", f"${saved:.4f}", delta=f"-{rate}%", delta_color="inverse")
        c3.metric("번역된 기사",          f"{savings.get('translated_articles', 0):,} 건")
        c4.metric("번역 스킵 기사",       f"{savings.get('skipped_articles', 0):,} 건")

        # ── 상세 breakdown ──
        with st.expander("비용 상세 내역", expanded=False):
            col_a, col_b = st.columns(2)

            with col_a:
                st.markdown("**실제 지출**")
                st.write(f"- 입력 토큰 비용: `${cost_d.get('actual_input_usd', 0.0):.6f}`")
                st.write(f"- 출력 토큰 비용: `${cost_d.get('actual_output_usd', 0.0):.6f}`")
                st.write(f"- **합계: `${actual:.6f}`**")
                st.write(f"- 평균 응답 시간: `{usage.get('avg_latency_ms', 0):.0f} ms`")

            with col_b:
                st.markdown("**Priority 절감 추정 (로직)**")
                avg_tok = savings.get("avg_tokens_per_call", 0)
                skipped = savings.get("skipped_articles",    0)
                st.write(f"- 번역 스킵된 기사: `{skipped:,} 건`")
                st.write(f"- 기사당 평균 토큰: `{avg_tok:,.0f}`")
                st.write(f"- 절감 토큰 추정: `{savings.get('saved_tokens_est', 0):,}`")
                st.write(f"- 절감 비용 추정: **`${saved:.6f}`**")
                st.write(f"- Priority 없을 시 예상 비용: `${total_if:.6f}`")
    else:
        st.info("비용 데이터를 불러올 수 없습니다. DB 연결을 확인하세요.")


# ─────────────────────────────────────────────────────────────
# TAB 2: 스크래핑 제어
# ─────────────────────────────────────────────────────────────

with tab_scrape:
    st.header("날짜 범위 스크래핑")
    st.caption(
        "시작일과 종료일을 지정하면 해당 기간의 기사를 수집합니다. "
        "백그라운드에서 실행되므로 결과는 대시보드에서 확인하세요."
    )

    with st.form("scrape_form"):
        col_start, col_end = st.columns(2)
        start_date = col_start.date_input(
            "시작일",
            value=date.today() - timedelta(days=1),
            max_value=date.today(),
        )
        end_date = col_end.date_input(
            "종료일",
            value=date.today(),
            max_value=date.today(),
        )

        col_lang, col_pages = st.columns(2)
        lang_label = col_lang.selectbox("언어", list(LANGUAGE_OPTIONS.keys()))
        max_pages  = col_pages.number_input(
            "최대 수집 페이지 수", min_value=1, max_value=200, value=10, step=5
        )

        st.divider()
        dry_run = st.toggle(
            "드라이 런 (테스트 모드)",
            value=False,
            help="켜면 스크래핑·파싱은 수행하지만 DB 에 저장하지 않습니다.",
        )
        if dry_run:
            st.info("**테스트 모드 활성화** — 결과가 DB 에 저장되지 않습니다.", icon="🧪")

        btn_label      = "🧪 드라이 런 시작" if dry_run else "🕷️ 스크래핑 시작"
        scrape_submit  = st.form_submit_button(btn_label, type="primary", use_container_width=True)

    if scrape_submit:
        if end_date < start_date:
            st.warning("종료일이 시작일보다 앞설 수 없습니다.")
        else:
            result = _api("POST", "/scrape", json={
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date":   end_date.strftime("%Y-%m-%d"),
                "language":   LANGUAGE_OPTIONS[lang_label],
                "max_pages":  int(max_pages),
                "dry_run":    dry_run,
            })
            if result:
                task_id = result.get("task_id", "")
                st.success(
                    f"{'🧪 드라이 런' if dry_run else '✅'} 스크래핑 시작됨!\n\n"
                    f"**Task ID**: `{task_id}`\n\n"
                    f"기간: **{start_date}** ~ **{end_date}**  |  최대 {max_pages} 페이지"
                )
                st.session_state["last_scrape_task_id"] = task_id

    # ── 태스크 상태 조회 ──
    st.divider()
    st.subheader("태스크 상태 조회")

    last_tid = st.session_state.get("last_scrape_task_id", "")
    task_id_input = st.text_input(
        "Task ID",
        value=last_tid,
        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    )

    if st.button("🔍 상태 조회", disabled=not task_id_input):
        task = _api("GET", f"/scrape/{task_id_input.strip()}")
        if task:
            status_val = task.get("status", "")
            icon = {
                "pending":   "🕐",
                "running":   "🔄",
                "completed": "✅",
                "failed":    "❌",
            }.get(status_val, "❓")

            st.metric("상태", f"{icon} {status_val.upper()}")

            col_t1, col_t2 = st.columns(2)
            col_t1.write(f"**생성**: {task.get('created_at', '—')}")
            col_t2.write(f"**시작**: {task.get('started_at', '—')}")
            if task.get("completed_at"):
                st.write(f"**완료**: {task['completed_at']}")

            if task.get("result"):
                res = task["result"]
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("전체",   res.get("total",         0))
                r2.metric("성공",   res.get("success_count", 0))
                r3.metric("실패",   res.get("failed_count",  0))
                r4.metric("스킵",   res.get("skipped_count", 0))

            if task.get("error"):
                st.error(f"오류: {task['error']}")

            req_info = task.get("request", {})
            if req_info:
                with st.expander("요청 파라미터"):
                    st.json(req_info)


# ─────────────────────────────────────────────────────────────
# TAB 3: DB Queue 방식 — 비동기 작업 추가
# ─────────────────────────────────────────────────────────────

with tab_queue:
    st.header("작업 큐에 스크래핑 작업 추가")
    st.caption("EC2 워커가 10초 간격으로 큐를 폴링하여 자동 처리합니다.")

    with st.form("queue_form"):
        source_url = st.text_input(
            "기사 URL *",
            placeholder="https://tenasia.hankyung.com/...",
        )

        col1, col2 = st.columns(2)
        lang_label  = col1.selectbox("언어", list(LANGUAGE_OPTIONS.keys()))
        priority    = col2.slider("우선순위", min_value=1, max_value=10, value=5)

        platforms = st.multiselect(
            "배포 플랫폼",
            PLATFORM_OPTIONS,
            default=["x", "instagram"],
        )

        max_retries = st.number_input("최대 재시도 횟수", min_value=0, max_value=10, value=3)

        st.divider()
        dry_run = st.toggle(
            "드라이 런 (테스트 모드)",
            value=False,
            help=(
                "켜면 실제 스크래핑·파싱은 수행하되 DB 에 저장하지 않습니다.\n"
                "수집 결과(제목, 날짜 등)는 [DRY RUN] 태그로 로그에 출력됩니다."
            ),
        )
        if dry_run:
            st.info(
                "**테스트 모드 활성화** — 기사를 스크래핑하지만 DB 에 저장되지 않습니다.",
                icon="🧪",
            )

        btn_label = "🧪 드라이 런 시작" if dry_run else "📥 큐에 추가"
        submitted = st.form_submit_button(btn_label, type="primary", use_container_width=True)

    if submitted:
        if not source_url.strip():
            st.warning("기사 URL을 입력해 주세요.")
        else:
            result = _api("POST", "/jobs", json={
                "source_url":  source_url.strip(),
                "language":    LANGUAGE_OPTIONS[lang_label],
                "platforms":   platforms,
                "priority":    priority,
                "max_retries": max_retries,
                "dry_run":     dry_run,
            })
            if result:
                if dry_run:
                    st.success(
                        f"🧪 드라이 런 작업 추가 완료! Job ID: **{result['job_id']}** "
                        f"(DB 저장 없음)"
                    )
                    st.info(
                        "EC2 워커가 스크래핑·파싱을 수행하고 [DRY RUN] 로그를 출력합니다."
                    )
                else:
                    st.success(f"✅ 작업 추가 완료! Job ID: **{result['job_id']}**")
                    st.info("EC2 워커가 자동으로 작업을 가져가 처리합니다.")
                time.sleep(1)
                st.rerun()


# ─────────────────────────────────────────────────────────────
# TAB 4: SSM SendCommand 방식 — 즉시 실행
# ─────────────────────────────────────────────────────────────

with tab_ssm:
    st.header("EC2 스크래퍼 즉시 실행 (SSM SendCommand)")
    st.caption(
        "버튼을 누르면 AWS SSM 을 통해 EC2 인스턴스에 직접 명령을 전송합니다.\n"
        "큐를 거치지 않으므로 즉각 실행되지만, 결과 수신에 약간의 지연이 있습니다."
    )

    col_left, col_right = st.columns([1, 1])

    # ── 루프 재시작 ──
    with col_left:
        st.subheader("워커 재시작")
        st.caption("systemctl restart tih-scraper")
        if st.button("🔁 워커 재시작", use_container_width=True):
            result = _api("POST", "/trigger/ssm", json={"comment": "UI — restart worker"})
            if result:
                st.success(f"명령 전송 완료\nCommand ID: `{result['command_id']}`")
                st.session_state["last_command_id"] = result["command_id"]

    # ── Job ID 지정 실행 ──
    with col_right:
        st.subheader("특정 작업 즉시 실행")
        st.caption("python -m scraper.worker --job-id <id>")
        with st.form("ssm_job_form"):
            job_id_input = st.number_input("Job ID", min_value=1, step=1)
            ssm_submitted = st.form_submit_button("⚡ 즉시 실행", use_container_width=True)

        if ssm_submitted:
            result = _api("POST", "/trigger/ssm", json={
                "job_id":  int(job_id_input),
                "comment": f"UI — run job {job_id_input}",
            })
            if result:
                st.success(f"명령 전송 완료\nCommand ID: `{result['command_id']}`")
                st.session_state["last_command_id"] = result["command_id"]

    # ── SSM 실행 결과 조회 ──
    st.divider()
    st.subheader("명령 실행 결과 조회")

    last_cmd = st.session_state.get("last_command_id", "")
    command_id_input = st.text_input("Command ID", value=last_cmd, placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    if st.button("🔍 결과 조회", disabled=not command_id_input):
        res = _api("GET", f"/trigger/ssm/{command_id_input.strip()}")
        if res:
            status_emoji = {"Success": "✅", "InProgress": "🔄", "Failed": "❌"}.get(res["status"], "❓")
            st.metric("상태", f"{status_emoji} {res['status']}", delta=res.get("status_details"))

            if res.get("stdout"):
                with st.expander("표준 출력 (stdout)"):
                    st.code(res["stdout"])
            if res.get("stderr"):
                with st.expander("오류 출력 (stderr)", expanded=True):
                    st.code(res["stderr"])


# ─────────────────────────────────────────────────────────────
# TAB 5: 작업 히스토리
# ─────────────────────────────────────────────────────────────

with tab_history:
    st.header("작업 히스토리")

    col_filter, col_limit = st.columns([3, 1])
    status_filter = col_filter.selectbox(
        "상태 필터",
        ["전체", "pending", "running", "completed", "failed", "cancelled"],
    )
    limit = col_limit.number_input("표시 개수", min_value=5, max_value=100, value=20, step=5)

    jobs = _api("GET", f"/jobs?limit={limit}")

    if jobs is not None:
        if status_filter != "전체":
            jobs = [j for j in jobs if j.get("status") == status_filter]

        if not jobs:
            st.info("표시할 작업이 없습니다.")
        else:
            for job in jobs:
                _status = job.get("status", "")
                _icon = {
                    "pending":   "🕐",
                    "running":   "🔄",
                    "completed": "✅",
                    "failed":    "❌",
                    "cancelled": "🚫",
                }.get(_status, "❓")

                params   = job.get("params") or {}
                url      = params.get("source_url", "—")
                lang     = params.get("language", "—")
                retries  = job.get("retry_count", 0)
                max_r    = job.get("max_retries", 3)
                is_dry   = params.get("dry_run", False)
                dry_tag  = " 🧪" if is_dry else ""

                with st.expander(
                    f"{_icon} **#{job['id']}** | {_status.upper()}{dry_tag} | {url[:60]}{'…' if len(url) > 60 else ''}",
                    expanded=False,
                ):
                    if is_dry:
                        st.warning("🧪 드라이 런 작업 — DB 에 저장되지 않습니다.", icon="🧪")

                    c1, c2, c3 = st.columns(3)
                    c1.write(f"**언어**: {lang}")
                    c2.write(f"**우선순위**: {job.get('priority')}")
                    c3.write(f"**재시도**: {retries}/{max_r}")

                    c1.write(f"**생성**: {job.get('created_at', '—')}")
                    c2.write(f"**시작**: {job.get('started_at', '—')}")
                    c3.write(f"**완료**: {job.get('completed_at', '—')}")

                    if job.get("worker_id"):
                        st.caption(f"Worker: `{job['worker_id']}`")

                    if job.get("error_msg"):
                        st.error(f"오류: {job['error_msg']}")

                    if job.get("result"):
                        with st.expander("결과 JSON"):
                            st.json(job["result"])

                    # 작업 취소 버튼 (pending 만)
                    if _status == "pending":
                        if st.button(f"🚫 취소 (#{job['id']})", key=f"cancel_{job['id']}"):
                            cancel_result = _api("DELETE", f"/jobs/{job['id']}")
                            if cancel_result:
                                st.success("취소되었습니다.")
                                time.sleep(0.5)
                                st.rerun()


# ─────────────────────────────────────────────────────────────
# TAB 6: 기사 뷰어
# ─────────────────────────────────────────────────────────────

# 언어 토글 현재 값 (사이드바 라디오 → session_state)
_is_en = st.session_state.get("lang_display", "KO") == "EN"

# 상태 배지 색상 매핑
_STATUS_COLOR = {
    "PROCESSED":     "green",
    "MANUAL_REVIEW": "orange",
    "SCRAPED":       "blue",
    "PENDING":       "gray",
    "ERROR":         "red",
}

_STATUS_ICON = {
    "PROCESSED":     "✅",
    "MANUAL_REVIEW": "🔍",
    "SCRAPED":       "📄",
    "PENDING":       "🕐",
    "ERROR":         "❌",
}


def _safe_url(url: str | None) -> str:
    """XSS 방지: http/https URL 만 허용합니다."""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    return ""


def _lazy_thumb_html(img_url: str, link_url: str, size: int = 72) -> str:
    """
    브라우저 네이티브 lazy loading (<img loading="lazy">)을 사용하는
    클릭 가능한 썸네일 HTML 문자열을 반환합니다.
    img_url 이 유효하지 않으면 빈 문자열을 반환합니다.
    """
    safe_img  = _safe_url(img_url)
    safe_link = _safe_url(link_url)
    if not safe_img:
        return ""
    img_tag = (
        f'<img src="{safe_img}" loading="lazy" '
        f'width="{size}" height="{size}" '
        f'style="object-fit:cover;border-radius:6px;border:1px solid #dde;display:block;" '
        f'onerror="this.style.display=\'none\'" />'
    )
    if safe_link:
        return (
            f'<a href="{safe_link}" target="_blank" rel="noopener noreferrer"'
            f' style="display:block;">{img_tag}</a>'
        )
    return img_tag


def _render_article_card(article: dict, key_prefix: str) -> None:
    """
    기사 한 건의 카드를 렌더링합니다.

    - 선택된 언어(KO/EN)에 맞는 제목·요약을 우선 표시합니다.
    - EN 선택 시 title_en 이 비어 있으면 KO 제목으로 폴백하고 경고 배지를 표시합니다.
    - 카드 왼쪽에 S3 썸네일을 lazy load 방식으로 표시하며, 클릭 시 원문 URL 로 이동합니다.
    - 카드 하단에 영문 번역 수정 폼(title_en, summary_en)을 포함합니다.
    """
    article_id  = article["id"]
    status      = article.get("process_status", "")
    status_icon = _STATUS_ICON.get(status, "❓")

    # ── 표시 언어 선택 ───────────────────────────────────────
    title_ko    = article.get("title_ko")    or ""
    title_en    = article.get("title_en")    or ""
    summary_ko  = article.get("summary_ko")  or ""
    summary_en  = article.get("summary_en")  or ""
    artist_ko   = article.get("artist_name_ko") or ""
    artist_en   = article.get("artist_name_en") or ""
    tags        = article.get("hashtags_en" if _is_en else "hashtags_ko") or []

    has_en      = bool(title_en)
    title_disp  = (title_en  if has_en else title_ko) if _is_en else title_ko
    summ_disp   = (summary_en if summary_en else summary_ko) if _is_en else summary_ko
    artist_disp = (artist_en  if artist_en  else artist_ko)  if _is_en else artist_ko

    # 번역 누락 경고 플래그
    missing_en = _is_en and not has_en

    # ── 썸네일 / 원본 URL ────────────────────────────────────
    # S3 처리 썸네일을 우선 사용하고, 없으면 원본 URL 로 폴백
    thumb_url  = _safe_url(article.get("thumbnail_s3_url") or article.get("thumbnail_url") or "")
    source_url = _safe_url(article.get("source_url") or "")

    # ── 카드 헤더 ────────────────────────────────────────────
    expander_label = (
        f"{status_icon} **#{article_id}** "
        f"{'⚠️ ' if missing_en else ''}"
        f"| {status} "
        f"| {title_disp[:70]}{'…' if len(title_disp) > 70 else ''}"
    )

    # ── 외부 레이아웃: [썸네일] | [기사 상세] | [링크 버튼] ──────
    col_thumb, col_main, col_link = st.columns([1, 8, 1])

    with col_thumb:
        if thumb_url:
            st.markdown(
                _lazy_thumb_html(thumb_url, source_url or thumb_url, size=72),
                unsafe_allow_html=True,
            )

    with col_link:
        if source_url:
            st.link_button("🔗", source_url, help="원문 보기")

    with col_main:
        with st.expander(expander_label, expanded=False):

            # ── 번역 누락 경고 배너 ──────────────────────────
            if missing_en:
                st.warning(
                    "영문 번역(title_en)이 없습니다. 아래 수정 폼에서 직접 입력하거나 "
                    "AI 재처리를 요청하세요.",
                    icon="⚠️",
                )

            # ── 기사 상세 ────────────────────────────────────
            col_meta, col_tags = st.columns([3, 2])

            with col_meta:
                if title_disp:
                    st.markdown(f"**{title_disp}**")
                if summ_disp:
                    st.caption(summ_disp)

                c1, c2, c3 = st.columns(3)
                c1.write(f"**아티스트**: {artist_disp or '—'}")
                c2.write(f"**언어**: {article.get('language', '—')}")
                c3.write(f"**상태**: {status_icon} {status}")

                c1.write(f"**작성자**: {article.get('author') or '—'}")
                c2.write(f"**발행**: {(article.get('published_at') or '—')[:10]}")
                c3.write(f"**수집**: {(article.get('created_at') or '—')[:10]}")

            with col_tags:
                if tags:
                    st.markdown("**해시태그**")
                    st.markdown(" ".join(f"`#{t}`" for t in tags[:10]))

            # KO/EN 제목·요약 모두 보기 (폴드)
            with st.expander("KO / EN 원문 비교", expanded=False):
                ka, ea = st.columns(2)
                ka.markdown("**한국어**")
                ka.write(title_ko  or "—")
                ka.caption(summary_ko or "")
                ea.markdown("**English**")
                ea.write(title_en  or "*(번역 없음)*")
                ea.caption(summary_en or "")

            st.divider()

            # ── 수동 번역 수정 폼 ────────────────────────────
            st.markdown("**✏️ 영문 번역 수동 수정**")

            with st.form(key=f"{key_prefix}_edit_{article_id}"):
                new_title_en = st.text_input(
                    "영문 제목 (title_en)",
                    value=title_en,
                    placeholder="Enter English title…",
                )
                new_summary_en = st.text_area(
                    "영문 요약 (summary_en)",
                    value=summary_en,
                    placeholder="Enter English summary…",
                    height=120,
                )

                col_save, col_clear = st.columns([3, 1])
                save_btn  = col_save.form_submit_button(
                    "💾 저장", type="primary", use_container_width=True
                )
                clear_btn = col_clear.form_submit_button(
                    "🗑 비우기", use_container_width=True
                )

        if save_btn:
            result = _api(
                "PATCH",
                f"/articles/{article_id}",
                json={"title_en": new_title_en, "summary_en": new_summary_en},
            )
            if result:
                st.success(f"article #{article_id} 저장 완료.")
                time.sleep(0.4)
                st.rerun()

        if clear_btn:
            result = _api(
                "PATCH",
                f"/articles/{article_id}",
                json={"title_en": "", "summary_en": ""},
            )
            if result:
                st.info(f"article #{article_id} 번역 필드를 비웠습니다.")
                time.sleep(0.4)
                st.rerun()


with tab_articles:
    st.header("기사 뷰어")
    st.caption(
        "사이드바의 [KO / EN] 토글로 표시 언어를 전환합니다. "
        "EN 선택 시 번역이 없는 기사는 ⚠️ 배지와 함께 KO 제목으로 폴백 표시됩니다."
    )

    # ── 공통 필터 ─────────────────────────────────────────────
    with st.expander("필터 / 표시 설정", expanded=False):
        f_col1, f_col2, f_col3 = st.columns(3)
        art_status_filter = f_col1.selectbox(
            "처리 상태",
            ["전체", "PROCESSED", "MANUAL_REVIEW", "SCRAPED", "PENDING", "ERROR"],
            key="art_status_filter",
        )
        art_limit = f_col2.number_input(
            "표시 개수", min_value=5, max_value=200, value=30, step=10,
            key="art_limit",
        )
        f_col3.markdown("")
        f_col3.markdown("")
        art_refresh = f_col3.button("🔄 새로고침", key="art_refresh")

    status_param = None if art_status_filter == "전체" else art_status_filter
    limit_param  = int(art_limit)

    # ── 서브 탭 ───────────────────────────────────────────────
    sub_all, sub_pending = st.tabs(["📰 전체 기사", "⏳ Translation Pending"])

    # ── 전체 기사 탭 ──────────────────────────────────────────
    with sub_all:
        params_all: dict = {"limit": limit_param}
        if status_param:
            params_all["process_status"] = status_param

        articles_all = _api("GET", "/articles", params=params_all)

        if articles_all is None:
            st.warning("기사 목록을 불러올 수 없습니다. API 서버 연결을 확인하세요.")
        elif not articles_all:
            st.info("조건에 맞는 기사가 없습니다.")
        else:
            st.caption(f"총 **{len(articles_all)}** 건 표시 중 (표시 언어: **{st.session_state.get('lang_display', 'KO')}**)")
            for art in articles_all:
                _render_article_card(art, key_prefix="all")

    # ── Translation Pending 탭 ────────────────────────────────
    with sub_pending:
        params_pend: dict = {"translation_pending": "true", "limit": limit_param}
        if status_param:
            params_pend["process_status"] = status_param

        articles_pend = _api("GET", "/articles", params=params_pend)

        if articles_pend is None:
            st.warning("기사 목록을 불러올 수 없습니다. API 서버 연결을 확인하세요.")
        elif not articles_pend:
            st.success("번역 누락 기사가 없습니다. 모든 기사에 영문 번역이 완료되었습니다.")
        else:
            st.error(
                f"**{len(articles_pend)}건**의 기사에 영문 번역(title_en)이 없습니다. "
                "아래에서 직접 수정하거나 AI 재처리를 진행하세요.",
                icon="⚠️",
            )
            for art in articles_pend:
                _render_article_card(art, key_prefix="pend")


# ─────────────────────────────────────────────────────────────
# TAB 7: Glossary 관리
# ─────────────────────────────────────────────────────────────

_CAT_LABEL = {"ARTIST": "🎤 아티스트", "AGENCY": "🏢 소속사", "EVENT": "🎪 이벤트"}
_CAT_COLOR = {"ARTIST": "blue", "AGENCY": "green", "EVENT": "orange"}

with tab_glossary:
    st.header("Glossary 관리")
    st.caption("AI 번역 프롬프트에 주입되는 한↔영 고유명사 사전입니다. 등록된 용어는 다음 번역 시 즉시 반영됩니다.")

    # ── 신규 등록 폼 ──────────────────────────────────────────
    with st.expander("➕ 새 용어 등록", expanded=False):
        with st.form("glossary_create_form"):
            gc1, gc2 = st.columns(2)
            new_term_ko  = gc1.text_input("한국어 원어 *", placeholder="예: 방탄소년단")
            new_term_en  = gc2.text_input("영어 표기",     placeholder="예: BTS")
            new_cat      = gc1.selectbox("분류 *", ["ARTIST", "AGENCY", "EVENT"])
            new_desc     = gc2.text_input("설명 (선택)", placeholder="예: 7인조 보이그룹, 2013 데뷔")
            create_btn   = st.form_submit_button("✅ 등록", type="primary", use_container_width=True)

        if create_btn:
            if not new_term_ko.strip():
                st.warning("한국어 원어를 입력해 주세요.")
            else:
                res = _api("POST", "/glossary", json={
                    "term_ko":     new_term_ko.strip(),
                    "term_en":     new_term_en.strip() or None,
                    "category":    new_cat,
                    "description": new_desc.strip() or None,
                })
                if res:
                    st.success(f"등록 완료 (id={res['id']})")
                    time.sleep(0.3)
                    st.rerun()

    st.divider()

    # ── 검색 & 필터 ───────────────────────────────────────────
    sf1, sf2, sf3 = st.columns([3, 2, 1])
    gl_search = sf1.text_input("검색 (한국어 원어)", placeholder="검색어를 입력하세요…", key="gl_search")
    gl_cat    = sf2.selectbox("분류 필터", ["전체", "ARTIST", "AGENCY", "EVENT"], key="gl_cat")
    sf3.markdown("")
    sf3.markdown("")
    gl_refresh = sf3.button("🔄", key="gl_refresh", help="새로고침")

    params_gl: dict = {}
    if gl_search:
        params_gl["q"] = gl_search
    if gl_cat != "전체":
        params_gl["category"] = gl_cat

    glossary_items = _api("GET", "/glossary", params=params_gl)

    if glossary_items is None:
        st.warning("Glossary를 불러올 수 없습니다.")
    elif not glossary_items:
        st.info("등록된 용어가 없습니다.")
    else:
        st.caption(f"총 **{len(glossary_items)}** 건")

        for g in glossary_items:
            gid  = g["id"]
            cat  = g.get("category", "")
            label = (
                f":{_CAT_COLOR.get(cat, 'gray')}[{_CAT_LABEL.get(cat, cat)}]"
                f"  **{g['term_ko']}**  →  {g['term_en'] or '*(미입력)*'}"
                f"{'  ·  ' + g['description'][:40] if g.get('description') else ''}"
            )
            with st.expander(label, expanded=False):
                with st.form(key=f"gl_edit_{gid}"):
                    e1, e2 = st.columns(2)
                    edit_ko   = e1.text_input("한국어 원어", value=g["term_ko"])
                    edit_en   = e2.text_input("영어 표기",   value=g["term_en"] or "")
                    edit_cat  = e1.selectbox(
                        "분류",
                        ["ARTIST", "AGENCY", "EVENT"],
                        index=["ARTIST", "AGENCY", "EVENT"].index(cat) if cat in ["ARTIST","AGENCY","EVENT"] else 0,
                    )
                    edit_desc = e2.text_input("설명", value=g.get("description") or "")

                    col_upd, col_del = st.columns([3, 1])
                    upd_btn = col_upd.form_submit_button("💾 수정", use_container_width=True)
                    del_btn = col_del.form_submit_button("🗑 삭제", use_container_width=True, type="secondary")

                if upd_btn:
                    res = _api("PUT", f"/glossary/{gid}", json={
                        "term_ko":     edit_ko.strip()   or None,
                        "term_en":     edit_en.strip()   or None,
                        "category":    edit_cat,
                        "description": edit_desc.strip() or None,
                    })
                    if res:
                        st.success("수정 완료")
                        time.sleep(0.3)
                        st.rerun()

                if del_btn:
                    res = _api("DELETE", f"/glossary/{gid}")
                    if res:
                        st.success(f"id={gid} 삭제 완료")
                        time.sleep(0.3)
                        st.rerun()


# ─────────────────────────────────────────────────────────────
# TAB 8: 아티스트 관리 (우선순위 설정)
# ─────────────────────────────────────────────────────────────

_PRIORITY_LABEL = {
    1: "1 — 전체 번역",
    2: "2 — 요약만",
    3: "3 — 번역 제외",
}
_PRIORITY_COLOR = {1: "green", 2: "orange", 3: "red", None: "gray"}
_PRIORITY_DESC  = {
    1: "title_en + summary_en + hashtags_en 전체 번역 (글로벌 팬덤 아티스트)",
    2: "summary_en 만 번역 (국내 인지도 있으나 글로벌 팬덤 제한)",
    3: "번역 없이 한국어 최소 추출만 (국내 아티스트 / 신인)",
}

with tab_artists:
    st.header("아티스트 우선순위 관리")
    st.caption(
        "글로벌 번역 우선순위는 Gemini AI 번역 비용 절감의 핵심 설정입니다. "
        "우선순위 변경은 **다음 번 스크래핑부터** 반영됩니다."
    )

    # ── 우선순위 범례 ──────────────────────────────────────────
    with st.expander("우선순위 설명", expanded=False):
        for p, desc in _PRIORITY_DESC.items():
            color = _PRIORITY_COLOR[p]
            st.markdown(f":{color}[**{_PRIORITY_LABEL[p]}**] — {desc}")
        st.markdown(":gray[**미분류(null)**] — 신규 등록 아티스트 초기 상태. 스크래핑 시 우선순위 1로 처리됨.")

    st.divider()

    # ── 검색 & 조회 ───────────────────────────────────────────
    ar1, ar2 = st.columns([4, 1])
    artist_search = ar1.text_input(
        "아티스트 검색 (한국어명)", placeholder="예: 아이유, BTS…", key="artist_search"
    )
    ar2.markdown("")
    ar2.markdown("")
    artist_refresh = ar2.button("🔄 조회", key="artist_refresh", use_container_width=True)

    params_ar: dict = {"limit": 50}
    if artist_search.strip():
        params_ar["q"] = artist_search.strip()

    artists_list = _api("GET", "/artists", params=params_ar)

    if artists_list is None:
        st.warning("아티스트 목록을 불러올 수 없습니다.")
    elif not artists_list:
        st.info("검색 결과가 없습니다." if artist_search else "등록된 아티스트가 없습니다.")
    else:
        # ── 우선순위별 요약 ──────────────────────────────────
        from collections import Counter
        prio_counts = Counter(a.get("global_priority") for a in artists_list)
        pm1, pm2, pm3, pm_n = st.columns(4)
        pm1.metric(":green[우선순위 1 (전체번역)]", prio_counts.get(1, 0))
        pm2.metric(":orange[우선순위 2 (요약만)]",   prio_counts.get(2, 0))
        pm3.metric(":red[우선순위 3 (번역제외)]",    prio_counts.get(3, 0))
        pm_n.metric(":gray[미분류]",                  prio_counts.get(None, 0))

        st.caption(f"총 **{len(artists_list)}** 명 표시")
        st.divider()

        # ── 아티스트 카드 목록 ────────────────────────────────
        for artist in artists_list:
            aid      = artist["id"]
            name_ko  = artist.get("name_ko", "—")
            name_en  = artist.get("name_en") or ""
            agency   = artist.get("agency")  or "—"
            cur_prio = artist.get("global_priority")
            verified = artist.get("is_verified", False)
            pcolor   = _PRIORITY_COLOR.get(cur_prio, "gray")
            plabel   = _PRIORITY_LABEL.get(cur_prio, "미분류")

            header = (
                f":{pcolor}[{plabel}]"
                f"  **{name_ko}**"
                f"{' (' + name_en + ')' if name_en else ''}"
                f"  ·  {agency}"
                f"{'  ✅' if verified else ''}"
            )

            with st.expander(header, expanded=False):
                col_info, col_ctrl = st.columns([2, 2])

                with col_info:
                    st.write(f"**ID**: {aid}")
                    st.write(f"**소속사**: {agency}")
                    st.write(f"**검증 여부**: {'✅ 검증됨' if verified else '미검증'}")
                    if artist.get("debut_date"):
                        st.write(f"**데뷔**: {artist['debut_date'][:10]}")

                with col_ctrl:
                    st.markdown("**우선순위 변경**")
                    with st.form(key=f"artist_prio_{aid}"):
                        options   = [None, 1, 2, 3]
                        opt_labels = ["미분류", "1 — 전체 번역", "2 — 요약만", "3 — 번역 제외"]
                        cur_idx   = options.index(cur_prio) if cur_prio in options else 0
                        new_prio_label = st.radio(
                            "우선순위",
                            opt_labels,
                            index=cur_idx,
                            horizontal=True,
                            label_visibility="collapsed",
                        )
                        save_prio = st.form_submit_button("💾 저장", use_container_width=True)

                    if save_prio:
                        new_prio_val = options[opt_labels.index(new_prio_label)]
                        res = _api(
                            "PATCH",
                            f"/artists/{aid}/priority",
                            json={"global_priority": new_prio_val},
                        )
                        if res:
                            st.success(f"**{name_ko}** 우선순위 → {new_prio_label}")
                            time.sleep(0.3)
                            st.rerun()
