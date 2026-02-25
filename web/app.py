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
from typing import Any

import requests
import streamlit as st

# ── 설정 ─────────────────────────────────────────────────────
API_BASE = "http://localhost:8000"

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
    if st.button("🔄 새로고침", use_container_width=True):
        st.rerun()


# ── 탭 레이아웃 ───────────────────────────────────────────────

tab_queue, tab_ssm, tab_history = st.tabs(
    ["📥 작업 큐 (비동기)", "⚡ 즉시 실행 (SSM)", "📋 작업 히스토리"]
)


# ─────────────────────────────────────────────────────────────
# TAB 1: DB Queue 방식 — 비동기 작업 추가
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
# TAB 2: SSM SendCommand 방식 — 즉시 실행
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
# TAB 3: 작업 히스토리
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
