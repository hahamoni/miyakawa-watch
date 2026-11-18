#!/usr/bin/env python3
"""
스시 미야카와(すし宮川) 취소표 감시기
- 공식 사이트 달력 데이터(src/calendar.json)를 확인해서
  지정한 날짜에 '空有り'(class: reservable)가 뜨면 텔레그램으로 알림.
- LOOP_MINUTES 동안 INTERVAL_SECONDS 간격(기본 60초)으로 반복 확인.
- 외부 라이브러리 없이 파이썬 표준 라이브러리만 사용.

환경변수
  TELEGRAM_BOT_TOKEN  : @BotFather 에서 받은 봇 토큰 (필수)
  TELEGRAM_CHAT_ID    : 알림 받을 내 chat id (필수)
  TARGET_DATES        : 감시할 날짜, 쉼표 구분 (기본: 2026-12-21,2026-12-22,2026-12-23)
  INTERVAL_SECONDS    : 확인 간격(초), 기본 60
  LOOP_MINUTES        : 반복할 총 시간(분), 기본 0 = 한 번만 확인
  GIT_COMMIT          : "true" 면 상태가 바뀔 때마다 state.json 을 커밋·푸시
  SEND_TEST           : "true" 면 현재 상태를 담은 테스트 메시지를 보냄
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CAL_URL = "https://www.sushi-miyakawa.com/src/calendar.json"
SITE_URL = "https://www.sushi-miyakawa.com/"
PHONE = "+81-11-613-2221"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
JST = timezone(timedelta(hours=9))
ERROR_ALERT_AFTER = 10  # 연속 실패가 이만큼 쌓이면 텔레그램으로 1회 경고

STATUS_KO = {
    "reservable": "빈자리 있음(空有り)",
    "temporary": "임시휴무",
    "regular": "정기휴무",
    None: "영업일 · 빈자리 없음",
}
WEEKDAY_KO = "월화수목금토일"


def log(msg: str) -> None:
    print(f"{datetime.now(JST):%m-%d %H:%M:%S} {msg}", flush=True)


def fmt_date(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}({WEEKDAY_KO[dt.weekday()]})"


def fetch_calendar() -> dict:
    """date -> class 맵. 같은 날짜에 여러 항목이 있으면 reservable 을 우선."""
    url = f"{CAL_URL}?t={int(time.time())}"  # 캐시 회피
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (miyakawa-cancel-watch)",
        "Cache-Control": "no-cache",
    })
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8-sig"))
            break
        except Exception as e:  # 네트워크 일시 오류 재시도
            last_err = e
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"calendar.json 가져오기 실패: {last_err}")

    status = {}
    for ev in data.get("monthly", []):
        d, c = ev.get("startdate"), ev.get("class")
        if not d:
            continue
        if status.get(d) == "reservable":
            continue
        status[d] = c
    return status


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read())
            if res.get("ok"):
                return
            last_err = res
        except Exception as e:
            last_err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"텔레그램 전송 실패: {last_err}")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    if os.environ.get("GIT_COMMIT", "").lower() == "true":
        try:
            run = lambda *a: subprocess.run(a, cwd=BASE_DIR, check=True,
                                            capture_output=True, text=True)
            run("git", "add", "state.json")
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR).returncode:
                run("git", "commit", "-m", "update state")
                run("git", "pull", "--rebase", "-q")
                run("git", "push", "-q")
        except Exception as e:  # 커밋 실패해도 감시는 계속
            log(f"[WARN] state 커밋 실패: {e}")


def check_once(targets: list, state: dict) -> bool:
    """한 번 확인. 상태가 바뀌었으면 True."""
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    status = fetch_calendar()
    changed = False

    for d in [t for t in targets if t >= today]:
        cur = status.get(d)
        prev_known = d in state
        prev = state.get(d)

        if cur == "reservable" and prev != "reservable":
            send_telegram(
                "🍣 스시 미야카와 빈자리 발생!\n\n"
                f"📅 {fmt_date(d)} — 공식 달력에 '空有り' 표시\n"
                f"📞 지금 바로 전화: {PHONE}\n"
                "   (전화로만 예약 · 대리 예약 불가 · 전날 취소 50%, 당일 100%)\n"
                f"🔗 {SITE_URL}\n\n"
                f"확인 시각: {now:%m/%d %H:%M} (한국·일본 시간)"
            )
            log(f"[ALERT] {d} reservable")
        elif prev == "reservable" and cur != "reservable":
            send_telegram(
                f"⌛ 스시 미야카와 {fmt_date(d)} 빈자리 표시가 사라졌어요.\n"
                f"현재 상태: {STATUS_KO.get(cur, cur)}"
            )
            log(f"[GONE] {d} -> {cur}")
        elif prev_known and cur in ("temporary", "regular") and prev not in ("temporary", "regular"):
            send_telegram(
                f"ℹ️ 스시 미야카와 {fmt_date(d)} 이(가) {STATUS_KO[cur]}로 바뀌었어요.")
            log(f"[CLOSED] {d} -> {cur}")

        if not prev_known or prev != cur:
            state[d] = cur
            changed = True

    if changed:
        save_state(state)
    log("checked " + ", ".join(f"{d[5:]}={status.get(d)}" for d in targets))
    return changed


def main() -> int:
    targets = [d.strip() for d in os.environ.get(
        "TARGET_DATES", "2026-12-21,2026-12-22,2026-12-23").split(",") if d.strip()]
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    loop_minutes = float(os.environ.get("LOOP_MINUTES", "0"))
    send_test = os.environ.get("SEND_TEST", "").lower() == "true"

    if send_test:
        status = fetch_calendar()
        lines = [f"• {fmt_date(d)}: {STATUS_KO.get(status.get(d), status.get(d))}" for d in targets]
        send_telegram(
            "✅ 미야카와 취소표 감시기 연결 테스트\n\n"
            "현재 공식 달력 상태:\n" + "\n".join(lines) +
            f"\n\n{interval}초마다 확인하고, 빈자리가 생기면 이 채팅으로 바로 알려드릴게요."
        )
        log("[TEST] sent")

    deadline = time.monotonic() + loop_minutes * 60
    state = load_state()
    errors = 0
    while True:
        if max(targets) < datetime.now(JST).strftime("%Y-%m-%d"):
            log("감시 대상 날짜가 모두 지났습니다. 워크플로를 꺼도 됩니다.")
            return 0
        started = time.monotonic()
        try:
            check_once(targets, state)
            errors = 0
        except Exception as e:
            errors += 1
            log(f"[ERROR {errors}] {e}")
            if errors == ERROR_ALERT_AFTER:
                try:
                    send_telegram(
                        f"⚠️ 미야카와 감시기: 달력 확인이 {errors}번 연속 실패했어요.\n"
                        f"사이트가 바뀌었거나 일시 장애일 수 있어요.\n오류: {e}")
                except Exception:
                    pass
            if loop_minutes <= 0:
                raise
        if loop_minutes <= 0:
            return 0
        sleep_for = interval - (time.monotonic() - started)
        if time.monotonic() + max(sleep_for, 0) >= deadline:
            log("이번 실행 종료 (다음 실행이 이어받습니다)")
            return 0
        time.sleep(max(sleep_for, 1))


if __name__ == "__main__":
    sys.exit(main())
