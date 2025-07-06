# -*- coding: utf-8 -*-
from mastodon import Mastodon, StreamListener
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import random
import pytz
import re
from mastodon import StreamListener
from concurrent.futures import ThreadPoolExecutor

# ===== 기본 설정 =====
KST = pytz.timezone("Asia/Seoul")
BOT_USERNAME = "autobot1"  # 계정 이름 맞게 바꿔
ACCESS_TOKEN = "MO8f2Wu12e65BGNrh9dN7A5qPA0XGYW50yapDbWI2KA"
INSTANCE_URL = "https://ullambana.xyz"

# ===== 마스토돈 연동 =====
try:
    mastodon = Mastodon(
        access_token=ACCESS_TOKEN,
        api_base_url=INSTANCE_URL
    )
    me = mastodon.account_verify_credentials()
    print(f"[LOGIN SUCCESS] Logged in as: @{me['acct']}")
except Exception as e:
    print(f"[LOGIN ERROR] {e}")
    exit(1)

# ===== 구글 시트 연동 =====
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet_script = client.open("조사 - 이벤트").worksheet("이벤트스크립트")
sheet_jointlog = client.open("조사 - 이벤트").worksheet("합동조사기록")

# ===== 병렬 처리 설정 =====
executor = ThreadPoolExecutor(max_workers=12)  # 최대 12개의 요청 동시 처리

sheet_limit = client.open("조사 - 이벤트").worksheet("참여횟수")

def get_today_key(keyword, date):
    return f"{keyword}_{date.strftime('%Y-%m-%d')}"

def get_user_limit_count(user, keyword, date):
    all_data = sheet_limit.get_all_records()
    today_key = get_today_key(keyword, date)

    headers = sheet_limit.row_values(1)
    if today_key not in headers:
        sheet_limit.update_cell(1, len(headers) + 1, today_key)
        headers.append(today_key)

    user_row = next((i + 2 for i, row in enumerate(all_data) if row.get("유저명") == user), None)
    if user_row is None:
        empty_row = [user] + [""] * (len(headers) - 1)
        sheet_limit.append_row(empty_row)
        return 0

    col_index = headers.index(today_key) + 1
    value = sheet_limit.cell(user_row, col_index).value
    return int(value) if value and value.isdigit() else 0

def increase_user_limit_count(user, keyword, date):
    headers = sheet_limit.row_values(1)
    today_key = get_today_key(keyword, date)

    if today_key not in headers:
        sheet_limit.update_cell(1, len(headers) + 1, today_key)
        headers.append(today_key)

    all_data = sheet_limit.get_all_records()
    user_row = next((i + 2 for i, row in enumerate(all_data) if row.get("유저명") == user), None)

    if user_row is None:
        empty_row = [user] + [""] * (len(headers) - 1)
        sheet_limit.append_row(empty_row)
        user_row = len(all_data) + 2

    col_index = headers.index(today_key) + 1
    current = get_user_limit_count(user, keyword, date)
    sheet_limit.update_cell(user_row, col_index, str(current + 1))

# ===== 스크립트 불러오기 =====
def load_event_scripts():
    data = sheet_script.get_all_records()
    scripts_by_keyword = {}
    for row in data:
        key = row.get("키워드", "").strip()
        typ = row.get("타입", "").strip()
        text = row.get("스크립트", "").strip()
        if key and text:
            scripts_by_keyword.setdefault(key, {"일반": [], "보상": []})["보상" if typ == "보상" else "일반"].append(text)
    return scripts_by_keyword

# ===== 합동조사 중복 확인 및 저장 =====
def has_already_joined_joint_event(user1, user2, date):
    date_str = date.strftime("%Y-%m-%d")
    records = sheet_jointlog.get_all_records()
    for row in records:
        if row.get("날짜") == date_str:
            if {row.get("유저1"), row.get("유저2")} == {user1, user2}:
                return True
    return False

def save_joint_event_record(user1, user2, date):
    sheet_jointlog.append_row([date.strftime("%Y-%m-%d"), user1, user2])

# ===== 보물찾기 처리기 =====
def handle_treasure_search(user, keyword, status_id, status):
    now = datetime.now(KST)
    print(f"[INFO] 처리 시작 → 유저: @{user}, 키워드: '{keyword}'")

    # ===== 횟수 제한 검사 =====
    count = get_user_limit_count(user, keyword, now.date())
    if count >= 3:
        mastodon.status_post(
            f"@{user}\n오늘의 '{keyword}'를 이미 3회 진행하셨습니다.",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return

    scripts = load_event_scripts()
    script_set = scripts.get(keyword, {})
    flavors = script_set.get("일반", [])
    rewards = script_set.get("보상", [])

    # 합동보물찾기: 상대 확인 및 중복 체크
    partner = None
    if keyword == "합동보물찾기":
        mentions = [m["acct"] for m in status["mentions"] if m["acct"] != BOT_USERNAME]
        print(f"[DEBUG] 멘션 목록: {mentions}")

        if not mentions:
            mastodon.status_post(f"@{user}\n합동 보물찾기를 하려면 상대 유저를 멘션하세요.", in_reply_to_id=status_id, visibility="unlisted")
            return
        partner = mentions[0]
        if partner == user:
            mastodon.status_post(f"@{user}\n자신과는 합동 보물찾기를 할 수 없습니다.", in_reply_to_id=status_id, visibility="unlisted")
            return
        # 상대도 3회 초과시 차단
        partner_count = get_user_limit_count(partner, keyword, now.date())
        if partner_count >= 3:
            mastodon.status_post(
                f"@{user}\n@{partner}님은 오늘 이미 '합동보물찾기'를 3회 진행하셨습니다.",
                in_reply_to_id=status_id,
                visibility="unlisted"
            )
            return
        if has_already_joined_joint_event(user, partner, now.date()):
            mastodon.status_post(f"@{user}\n오늘은 이미 @{partner}님과 합동 보물찾기를 했습니다.", in_reply_to_id=status_id, visibility="unlisted")
            return
        save_joint_event_record(user, partner, now.date())

    #출력

    all_scripts = (script_set.get("일반", []) or []) + (script_set.get("보상", []) or [])
    if not all_scripts:
        text = "출력 가능한 스크립트가 없습니다."
        print(f"[WARN] 스크립트 없음")
    else:
        selected = random.choice(all_scripts)
        if "[보물]" in selected:
            text = f"{selected}\n획득: 보물"
        else:
            text = selected
        print(f"[DEBUG] 출력됨: {text}")

    mastodon.status_post(f"@{user}\n{text}", in_reply_to_id=status_id, visibility="unlisted")

    # ===== 조사 횟수 +1 저장 =====
    increase_user_limit_count(user, keyword, now.date())

    if partner:
        increase_user_limit_count(partner, keyword, now.date())

# ===== 스트림 리스너 =====
class TreasureListener(StreamListener):
    def on_notification(self, notification):
        if notification["type"] != "mention":
            return
        status = notification["status"]
        user = status["account"]["acct"]
        content_raw = status["content"]
        content_text = re.sub('<.*?>', '', content_raw).strip()

        print(f"\n[MENTION] @{user} → '{content_text}'")

        for keyword in ["보물찾기", "합동보물찾기"]:
            if keyword in content_text:
                print(f"[KEYWORD DETECTED] '{keyword}'")
                handle_treasure_search(user, keyword, status["id"], status)
                break

# ===== 멘션 수신 감지 리스너 클래스 =====
class MentionListener(StreamListener):
    def on_notification(self, notification):
        if notification["type"] == "mention":
            status = notification["status"]
            user = status["account"]["acct"]
            content = status["content"]

            # 키워드 추출
            if "합동보물찾기" in content:
                keyword = "합동보물찾기"
            elif "보물찾기" in content:
                keyword = "보물찾기"
            else:
                return

            status_id = status["id"]
            print(f"[DEBUG] 유저: @{user}, 키워드: {keyword}")

            # 병렬 처리 시작
            executor.submit(handle_treasure_search, user, keyword, status_id, status)

# ===== 봇 실행 =====
def start_bot():
    print("[INFO] 보물찾기 봇 실행 중...")
    mastodon.stream_user(MentionListener())

if __name__ == "__main__":
    start_bot()