#!/usr/bin/env python3
"""
삿포로 스시 취소표 감시기 (스시 미야카와 + 스시 카이)

- 스시 미야카와: 공식 사이트 달력(src/calendar.json)에 '空有り'(class: reservable)가 뜨면 알림
- 스시 카이: TableCheck 빈자리 조회(availability_calendar_v2)에서 예약 가능 시간이 생기면 알림
- LOOP_MINUTES 동안 INTERVAL_SECONDS 간격(기본 60초)으로 반복 확인
- 외부 라이브러리 없이 파이썬 표준 라이브러리만 사용

환경변수
  TELEGRAM_BOT_TOKEN  : @BotFather 에서 받은 봇 토큰 (필수)
  TELEGRAM_CHAT_ID    : 알림 받을 내 chat id (필수)
  MIYAKAWA_DATES      : 미야카와 감시 날짜, 쉼표 구분 (기본: 2026-12-21,2026-12-22,2026-12-23)
                        (예전 이름 TARGET_DATES 도 인식)
  KAI_DATES           : 스시 카이 감시 날짜 (기본: 2026-12-21,2026-12-22). 비우면 감시 안 함
  KAI_PEOPLE          : 스시 카이 인원 (기본 2)
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
JST = timezone(timedelta(hours=9))
ERROR_ALERT_AFTER = 10  # 연속 실패가 이만큼 쌓이면 텔레그램으로 1회 경고
WEEKDAY_KO = "월화수목금토일"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# ── 스시 미야카와 ─────────────────────────────────────────────
MIYAKAWA_URL = "https://www.sushi-miyakawa.com/src/calendar.json"
MIYAKAWA_SITE = "https://www.sushi-miyakawa.com/"
MIYAKAWA_PHONE = "+81-11-613-2221"

# ── 스시 카이 (TableCheck) ───────────────────────────────────
KAI_API = "https://production.tablecheck.com/v2/hub/availability_calendar_v2"
KAI_SHOP_ID = "sushikai"
KAI_RESERVE = "https://www.tablecheck.com/ko/sushikai/reserve"
KAI_PHONE = "+81-90-9431-3017"
# 조회 기준 시각(JST). API가 기준 시각 ±2시간 슬롯을 돌려주므로 여러 개로 저녁 전체를 덮음
KAI_ANCHOR_HOURS_JST = [17, 19, 21]


def log(msg: str) -> None:
    print(f"{datetime.now(JST):%m-%d %H:%M:%S} {msg}", flush=True)


def fmt_date(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}({WEEKDAY_KO[dt.weekday()]})"


def env_dates(name: str, default: str, fallback_name: str = None) -> list:
    raw = os.environ.get(name)
    if raw is None and fallback_name:
        raw = os.environ.get(fallback_name)
    if raw is None:
        raw = default
    return [d.strip() for d in raw.split(",") if d.strip()]


def http(url: str, data: bytes = None, headers: dict = None, tries: int = 3):
    h = {"User-Agent": UA, "Cache-Control": "no-cache"}
    h.update(headers or {})
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # 네트워크 일시 오류 재시도
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url} 요청 실패: {last_err}")


# ── 상태 조회 ────────────────────────────────────────────────
MIYAKAWA_KO = {
    "reservable": "빈자리 있음(空有り)",
    "temporary": "임시휴무",
    "regular": "정기휴무",
    None: "영업일 · 빈자리 없음",
}


def miyakawa_status(dates: list) -> dict:
    """date -> 'reservable' | 'temporary' | 'regular' | None"""
    data = http(f"{MIYAKAWA_URL}?t={int(time.time())}")
    status = {}
    for ev in data.get("monthly", []):
        d, c = ev.get("startdate"), ev.get("class")
        if d and status.get(d) != "reservable":
            status[d] = c
    return {d: status.get(d) for d in dates}


def kai_status(dates: list) -> dict:
    """date -> 'closed' | 'full' | 'open:HH:MM,HH:MM' """
    people = os.environ.get("KAI_PEOPLE", "2")
    first = min(dates)
    slots = {d: set() for d in dates}
    seen = {d: False for d in dates}
    closed = set()
    for h in KAI_ANCHOR_HOURS_JST:
        anchor = datetime.strptime(first, "%Y-%m-%d").replace(hour=h, tzinfo=JST)
        body = json.dumps({
            "locale": "ko",
            "start_at": anchor.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "shop_id": KAI_SHOP_ID,
            "num_people": str(people),
        }).encode()
        res = http(KAI_API, data=body, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": "https://www.tablecheck.com", "Referer": "https://www.tablecheck.com/",
        })
        cal = res.get("availability_calendar") or {}
        if cal.get("type") != "success":
            raise RuntimeError(f"TableCheck 응답 이상: {str(res)[:200]}")
        closed.update(cal.get("closed_dates") or [])
        for d in dates:
            for t, v in ((cal.get("data") or {}).get(d) or {}).items():
                seen[d] = True
                if v.get("is_available"):
                    jt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(JST)
                    slots[d].add(jt.strftime("%H:%M"))
    out = {}
    for d in dates:
        if d in closed:
            out[d] = "closed"
        elif slots[d]:
            out[d] = "open:" + ",".join(sorted(slots[d]))
        elif not seen[d]:
            raise RuntimeError(f"TableCheck 응답에 {d} 데이터가 없음")
        else:
            out[d] = "full"
    return out


def kai_ko(s: str) -> str:
    if s == "closed":
        return "휴무"
    if s == "full":
        return "영업일 · 빈자리 없음"
    if s and s.startswith("open:"):
        return "빈자리 있음 (" + s[5:].replace(",", ", ") + ")"
    return str(s)


# ── 알림 메시지 ──────────────────────────────────────────────
def miyakawa_messages(d: str, prev, cur, prev_known: bool, now: datetime) -> list:
    msgs = []
    if cur == "reservable" and prev != "reservable":
        msgs.append(
            "🍣 스시 미야카와 빈자리 발생!\n\n"
            f"📅 {fmt_date(d)} — 공식 달력에 '空有り' 표시\n"
            f"📞 지금 바로 전화: {MIYAKAWA_PHONE}\n"
            "   (전화로만 예약 · 대리 예약 불가 · 전날 취소 50%, 당일 100%)\n"
            f"🔗 {MIYAKAWA_SITE}\n\n"
            f"확인 시각: {now:%m/%d %H:%M} (한국·일본 시간)")
    elif prev == "reservable" and cur != "reservable":
        msgs.append(f"⌛ 스시 미야카와 {fmt_date(d)} 빈자리 표시가 사라졌어요.\n"
                    f"현재 상태: {MIYAKAWA_KO.get(cur, cur)}")
    elif prev_known and cur in ("temporary", "regular") and prev not in ("temporary", "regular"):
        msgs.append(f"ℹ️ 스시 미야카와 {fmt_date(d)} 이(가) {MIYAKAWA_KO[cur]}로 바뀌었어요.")
    return msgs


def kai_messages(d: str, prev, cur, prev_known: bool, now: datetime) -> list:
    msgs = []
    was_open = bool(prev and str(prev).startswith("open:"))
    is_open = bool(cur and cur.startswith("open:"))
    if is_open and not was_open:
        times = cur[5:].replace(",", ", ")
        msgs.append(
            "🍣 스시 카이 빈자리 발생!\n\n"
            f"📅 {fmt_date(d)} {times} — {os.environ.get('KAI_PEOPLE', '2')}명 예약 가능\n"
            f"🔗 바로 온라인 예약: {KAI_RESERVE}\n"
            f"📞 당일 예약은 전화로만: {KAI_PHONE}\n"
            "   (카운터석 · 코스 약 2시간 · 강한 향수 금지 · 20분 지각 시 취소 처리)\n\n"
            f"확인 시각: {now:%m/%d %H:%M} (한국·일본 시간)")
    elif was_open and not is_open:
        msgs.append(f"⌛ 스시 카이 {fmt_date(d)} 빈자리가 사라졌어요.\n현재 상태: {kai_ko(cur)}")
    elif prev_known and cur == "closed" and prev != "closed":
        msgs.append(f"ℹ️ 스시 카이 {fmt_date(d)} 이(가) 휴무로 바뀌었어요.")
    return msgs


WATCHERS = [
    # (key, 이름, 날짜 env, 기본값, 예전 env, 상태함수, 메시지함수, 한글변환)
    ("miyakawa", "스시 미야카와", "MIYAKAWA_DATES", "2026-12-21,2026-12-22,2026-12-23", "TARGET_DATES",
     miyakawa_status, miyakawa_messages, lambda s: MIYAKAWA_KO.get(s, s)),
    ("kai", "스시 카이", "KAI_DATES", "2026-12-21,2026-12-22", None,
     kai_status, kai_messages, kai_ko),
]


# ── 텔레그램 / 상태 저장 ─────────────────────────────────────
def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode()
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=body)
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
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # 예전 버전(미야카와 전용)의 날짜 키를 새 형식으로 옮김
    for k in [k for k in state if ":" not in k]:
        state[f"miyakawa:{k}"] = state.pop(k)
    return state


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


# ── 메인 루프 ────────────────────────────────────────────────
def active_watchers():
    out = []
    for key, name, env, default, old_env, fn, msgfn, ko in WATCHERS:
        dates = env_dates(env, default, old_env)
        if dates:
            out.append((key, name, dates, fn, msgfn, ko))
    return out


def check_watcher(w, state: dict) -> bool:
    key, name, dates, fn, msgfn, ko = w
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    live = [d for d in dates if d >= today]
    if not live:
        return False
    status = fn(live)
    changed = False
    for d in live:
        sk = f"{key}:{d}"
        cur, prev_known, prev = status[d], sk in state, state.get(sk)
        for m in msgfn(d, prev, cur, prev_known, now):
            send_telegram(m)
            log(f"[NOTIFY] {name} {d}: {prev} -> {cur}")
        if not prev_known or prev != cur:
            state[sk] = cur
            changed = True
    if changed:
        save_state(state)
    log(f"{name}: " + ", ".join(f"{d[5:]}={status[d]}" for d in live))
    return changed


def main() -> int:
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    loop_minutes = float(os.environ.get("LOOP_MINUTES", "0"))
    send_test = os.environ.get("SEND_TEST", "").lower() == "true"
    watchers = active_watchers()

    if send_test:
        parts = []
        for key, name, dates, fn, msgfn, ko in watchers:
            try:
                st = fn(dates)
                lines = [f"• {fmt_date(d)}: {ko(st[d])}" for d in dates]
            except Exception as e:
                lines = [f"• ⚠️ 조회 실패: {str(e)[:150]}"]
            parts.append(f"[{name}]\n" + "\n".join(lines))
        send_telegram(
            "✅ 스시 취소표 감시기 연결 테스트\n\n" + "\n\n".join(parts) +
            f"\n\n{interval}초마다 확인하고, 빈자리가 생기면 이 채팅으로 바로 알려드릴게요.")
        log("[TEST] sent")

    deadline = time.monotonic() + loop_minutes * 60
    state = load_state()
    errors = {w[0]: 0 for w in watchers}
    while True:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        if all(max(w[2]) < today for w in watchers):
            log("감시 대상 날짜가 모두 지났습니다. 워크플로를 꺼도 됩니다.")
            return 0
        started = time.monotonic()
        for w in watchers:
            key, name = w[0], w[1]
            try:
                check_watcher(w, state)
                errors[key] = 0
            except Exception as e:
                errors[key] += 1
                log(f"[ERROR {name} {errors[key]}] {e}")
                if errors[key] == ERROR_ALERT_AFTER:
                    try:
                        send_telegram(
                            f"⚠️ {name} 확인이 {errors[key]}번 연속 실패했어요.\n"
                            f"사이트가 바뀌었거나 일시 장애일 수 있어요.\n오류: {str(e)[:300]}")
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
