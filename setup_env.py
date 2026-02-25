#!/usr/bin/env python3
"""
setup_env.py — TenAsia Intelligence Hub 환경 변수 대화형 설정 스크립트

기능:
  1. .env.example 복사 → .env 생성 (이미 존재하면 덮어쓰기 확인)
  2. 필수 환경 변수 대화형 입력 (GEMINI_API_KEY, DATABASE_URL)
  3. 선택 환경 변수 설정 (AWS 리전, 로그 레벨 등)
  4. 입력값 유효성 검증 후 .env 저장
  5. 설정 완료 요약 출력

실행:
    python setup_env.py
    python setup_env.py --force   # 기존 .env 덮어쓰기 강제
    python setup_env.py --minimal  # 필수 항목만 설정
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────
# 경로 상수
# ─────────────────────────────────────────────────────────────

ROOT        = Path(__file__).parent.resolve()
ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE    = ROOT / ".env"

# ─────────────────────────────────────────────────────────────
# ANSI 컬러 (Windows ANSI 지원 활성화)
# ─────────────────────────────────────────────────────────────

class _C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"


def _enable_ansi() -> bool:
    """Windows에서 ANSI 이스케이프 시퀀스를 활성화합니다."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


if not _enable_ansi() or not sys.stdout.isatty():
    # ANSI 미지원 환경: 색상 코드 비활성화
    for _attr in ("RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "CYAN", "WHITE"):
        setattr(_C, _attr, "")


# ─────────────────────────────────────────────────────────────
# 출력 헬퍼
# ─────────────────────────────────────────────────────────────

def _banner() -> None:
    print(f"""
{_C.CYAN}{_C.BOLD}╔══════════════════════════════════════════════════════╗
║      TenAsia Intelligence Hub — 환경 설정 마법사      ║
╚══════════════════════════════════════════════════════╝{_C.RESET}
""")


def _section(title: str) -> None:
    print(f"\n{_C.BOLD}{_C.BLUE}▶ {title}{_C.RESET}")
    print(f"{_C.DIM}{'─' * 52}{_C.RESET}")


def _ok(msg: str)   -> None: print(f"  {_C.GREEN}✔{_C.RESET}  {msg}")
def _warn(msg: str) -> None: print(f"  {_C.YELLOW}⚠{_C.RESET}  {msg}")
def _err(msg: str)  -> None: print(f"  {_C.RED}✘{_C.RESET}  {msg}", file=sys.stderr)
def _info(msg: str) -> None: print(f"  {_C.CYAN}·{_C.RESET}  {msg}")


# ─────────────────────────────────────────────────────────────
# .env 파일 파서 / 라이터
# ─────────────────────────────────────────────────────────────

def _parse_env(path: Path) -> dict[str, str]:
    """
    .env 파일을 파싱하여 {KEY: VALUE} 딕셔너리를 반환합니다.
    주석과 빈 줄은 건너뜁니다.
    """
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """
    .env 파일에서 지정된 키의 값만 업데이트합니다.
    기존 주석, 공백, 미수정 줄은 그대로 유지합니다.
    """
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.rstrip("\n\r")
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            # 인라인 주석 보존
            if "#" in stripped.split("=", 1)[1]:
                _, _, comment = stripped.partition("#")
                new_lines.append(f"{key}={updates[key]}  #{comment}\n")
            else:
                new_lines.append(f"{key}={updates[key]}\n")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # 파일에 없는 새 키는 맨 끝에 추가
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}\n")

    path.write_text("".join(new_lines), encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 입력 헬퍼
# ─────────────────────────────────────────────────────────────

def _ask(
    prompt:    str,
    default:   str = "",
    secret:    bool = False,
    validator: Optional[callable] = None,
    hint:      str = "",
) -> str:
    """
    사용자 입력을 받습니다.

    Args:
        prompt:    프롬프트 텍스트
        default:   Enter 시 기본값
        secret:    True 이면 입력값을 화면에 표시하지 않음 (API 키 등)
        validator: 유효성 검증 함수 (값 반환 또는 ValueError 발생)
        hint:      프롬프트 아래에 표시할 힌트
    """
    if hint:
        _info(f"{_C.DIM}{hint}{_C.RESET}")

    default_display = (
        f" [{_C.DIM}{'*' * min(len(default), 8) + '…' if default else '없음'}{_C.RESET}]"
        if default else ""
    )

    while True:
        try:
            label = f"  {_C.BOLD}{prompt}{_C.RESET}{default_display}: "
            if secret:
                raw = getpass.getpass(label)
            else:
                raw = input(label)
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{_C.YELLOW}설정을 취소했습니다.{_C.RESET}")
            sys.exit(0)

        value = raw.strip() or default

        if validator:
            try:
                value = validator(value)
            except ValueError as exc:
                _err(str(exc))
                continue

        return value


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Y/n 또는 y/N 형식으로 예/아니오를 묻습니다."""
    suffix = f"[{_C.BOLD}Y{_C.RESET}/n]" if default else f"[y/{_C.BOLD}N{_C.RESET}]"
    try:
        answer = input(f"  {prompt} {suffix}: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)
    if not answer:
        return default
    return answer.startswith("y")


# ─────────────────────────────────────────────────────────────
# 유효성 검증 함수
# ─────────────────────────────────────────────────────────────

def _validate_gemini_key(v: str) -> str:
    if not v:
        raise ValueError(
            "GEMINI_API_KEY는 필수입니다.\n"
            "     발급: https://aistudio.google.com/apikey"
        )
    if len(v) < 20:
        raise ValueError("API 키가 너무 짧습니다. 키를 다시 확인해 주세요.")
    return v


def _validate_database_url(v: str) -> str:
    if not v:
        return v   # 개발 환경에서는 빈 값 허용
    if not re.match(r"^(postgresql|postgres)(\+\w+)?://", v):
        raise ValueError(
            "DATABASE_URL 형식이 올바르지 않습니다.\n"
            "     올바른 형식: postgresql://user:password@host:5432/dbname"
        )
    return v


def _validate_log_level(v: str) -> str:
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    upper = v.upper()
    if upper not in allowed:
        raise ValueError(f"유효한 로그 레벨: {', '.join(sorted(allowed))}")
    return upper


def _validate_rpm_limit(v: str) -> str:
    try:
        n = int(v)
        if n < 1 or n > 2000:
            raise ValueError
        return str(n)
    except (ValueError, TypeError):
        raise ValueError("1에서 2000 사이의 정수를 입력해 주세요. (무료: 15, 유료: 2000)")


# ─────────────────────────────────────────────────────────────
# 단계별 설정
# ─────────────────────────────────────────────────────────────

def step_copy_env(force: bool) -> None:
    """Step 1: .env.example → .env 복사"""
    _section("1 / 4  파일 생성")

    if not ENV_EXAMPLE.exists():
        _err(".env.example 파일이 없습니다. 저장소를 다시 클론해 주세요.")
        sys.exit(1)

    if ENV_FILE.exists() and not force:
        _warn(".env 파일이 이미 존재합니다.")
        if not _ask_yes_no("기존 .env를 덮어쓰고 새로 설정하시겠습니까?", default=False):
            _info("기존 .env를 유지합니다. 설정을 계속 진행합니다.")
            return

    shutil.copy2(ENV_EXAMPLE, ENV_FILE)
    _ok(f".env 파일 생성 완료 ({ENV_FILE})")


def step_required(existing: dict[str, str]) -> dict[str, str]:
    """Step 2: 필수 환경 변수 입력"""
    _section("2 / 4  필수 환경 변수")
    updates: dict[str, str] = {}

    # ── GEMINI_API_KEY ────────────────────────────────────────
    print(f"\n  {_C.BOLD}GEMINI_API_KEY{_C.RESET} {_C.RED}(필수){_C.RESET}")
    current = existing.get("GEMINI_API_KEY", "")
    if current:
        _info(f"현재 설정됨: {'*' * 8}…{current[-4:]}")
        if not _ask_yes_no("새 키로 변경하시겠습니까?", default=False):
            _ok("기존 GEMINI_API_KEY 유지")
        else:
            current = ""

    if not current:
        gemini_key = _ask(
            prompt="GEMINI_API_KEY 입력",
            secret=True,
            validator=_validate_gemini_key,
            hint="발급 주소: https://aistudio.google.com/apikey",
        )
        updates["GEMINI_API_KEY"] = gemini_key
        _ok("GEMINI_API_KEY 설정 완료")

    # ── DATABASE_URL ──────────────────────────────────────────
    print(f"\n  {_C.BOLD}DATABASE_URL{_C.RESET} {_C.YELLOW}(로컬 개발 선택){_C.RESET}")
    default_db = existing.get(
        "DATABASE_URL",
        "postgresql://tih_admin:password@localhost:5432/tih",
    )
    _info("미입력 시 기본값 사용 (프로덕션에서는 반드시 입력 필요)")
    db_url = _ask(
        prompt="DATABASE_URL 입력",
        default=default_db,
        secret=False,
        validator=_validate_database_url,
        hint=f"기본값: {default_db}",
    )
    if db_url:
        updates["DATABASE_URL"] = db_url
        _ok("DATABASE_URL 설정 완료")

    return updates


def step_optional(existing: dict[str, str], minimal: bool) -> dict[str, str]:
    """Step 3: 선택 환경 변수 입력"""
    _section("3 / 4  선택 환경 변수")
    updates: dict[str, str] = {}

    if minimal:
        _info("--minimal 옵션: 선택 항목을 건너뜁니다.")
        return updates

    if not _ask_yes_no("선택 환경 변수를 지금 설정하시겠습니까?", default=False):
        _info("기본값으로 진행합니다.")
        return updates

    # AWS_REGION
    region = _ask(
        prompt="AWS_REGION",
        default=existing.get("AWS_REGION", "ap-northeast-2"),
        hint="AWS 리전 (기본: ap-northeast-2)",
    )
    if region != existing.get("AWS_REGION", "ap-northeast-2"):
        updates["AWS_REGION"] = region

    # LOG_LEVEL
    log_level = _ask(
        prompt="LOG_LEVEL",
        default=existing.get("LOG_LEVEL", "INFO"),
        validator=_validate_log_level,
        hint="DEBUG | INFO | WARNING | ERROR (기본: INFO)",
    )
    if log_level != existing.get("LOG_LEVEL", "INFO"):
        updates["LOG_LEVEL"] = log_level

    # GEMINI_RPM_LIMIT
    rpm = _ask(
        prompt="GEMINI_RPM_LIMIT",
        default=existing.get("GEMINI_RPM_LIMIT", "60"),
        validator=_validate_rpm_limit,
        hint="Gemini 분당 최대 호출 수. 무료: 15, 유료 Pay-as-you-go: 2000 (기본: 60)",
    )
    if rpm != existing.get("GEMINI_RPM_LIMIT", "60"):
        updates["GEMINI_RPM_LIMIT"] = rpm

    if updates:
        _ok(f"선택 항목 {len(updates)}개 설정 완료")
    else:
        _ok("변경 없음 — 기본값 유지")

    return updates


def step_summary(all_updates: dict[str, str]) -> None:
    """Step 4: 완료 요약 출력"""
    _section("4 / 4  설정 완료")

    if all_updates:
        _ok(f".env 파일 업데이트 완료 ({len(all_updates)}개 항목)")
        print()
        for key, val in all_updates.items():
            masked = ('*' * 8 + '…' + val[-4:]) if len(val) > 12 and "KEY" in key else val
            _info(f"{_C.BOLD}{key}{_C.RESET} = {_C.DIM}{masked}{_C.RESET}")
    else:
        _ok("설정 변경 없음 (기존 .env 유지)")

    print(f"""
{_C.GREEN}{_C.BOLD}✔ 환경 설정이 완료됐습니다!{_C.RESET}

  다음 단계:
  {_C.CYAN}1.{_C.RESET} DB 마이그레이션 실행
     {_C.DIM}alembic upgrade head{_C.RESET}

  {_C.CYAN}2.{_C.RESET} 로컬 개발 서버 실행
     {_C.DIM}streamlit run web/app.py{_C.RESET}

  {_C.CYAN}3.{_C.RESET} API 서버 실행 (별도 터미널)
     {_C.DIM}uvicorn web.api:app --reload --port 8000{_C.RESET}

  {_C.YELLOW}⚠  .env 파일은 절대 커밋하지 마세요 (.gitignore 처리됨){_C.RESET}
""")


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────

def main() -> None:
    # Python 버전 확인
    if sys.version_info < (3, 10):
        _err(f"Python 3.10 이상이 필요합니다. (현재: {sys.version})")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="TenAsia Intelligence Hub .env 대화형 설정 스크립트",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="기존 .env 파일이 있어도 무조건 덮어쓰기",
    )
    parser.add_argument(
        "--minimal", "-m",
        action="store_true",
        help="필수 항목(GEMINI_API_KEY, DATABASE_URL)만 설정",
    )
    args = parser.parse_args()

    _banner()

    # Step 1: 파일 생성
    step_copy_env(force=args.force)

    # 현재 .env 파일 파싱
    existing = _parse_env(ENV_FILE) if ENV_FILE.exists() else {}

    # Step 2: 필수 항목
    req_updates = step_required(existing)

    # Step 3: 선택 항목
    opt_updates = step_optional({**existing, **req_updates}, minimal=args.minimal)

    # .env 파일에 실제 저장
    all_updates = {**req_updates, **opt_updates}
    if all_updates:
        _write_env(ENV_FILE, all_updates)

    # Step 4: 요약
    step_summary(all_updates)


if __name__ == "__main__":
    main()
