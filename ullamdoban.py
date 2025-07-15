# -*- coding: utf-8 -*-
import os
import re
import json
import random
import threading
import queue
from datetime import datetime
from mastodon import Mastodon, StreamListener
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz

# ========= [기본 설정] ========= #
MASTODON_ACCESS_TOKEN = "YDCVgW8xs6_5yxqbEe_XjXNrXNaZpg9QR5w6XVBUiKQ"
MASTODON_API_BASE_URL = "https://ullambana.xyz"
KST = pytz.timezone("Asia/Seoul")

masto = Mastodon(
    access_token=MASTODON_ACCESS_TOKEN,
    api_base_url=MASTODON_API_BASE_URL
)

# ========= [시트 연동] ========= #
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

sheet_script = client.open("조사 - 이벤트").worksheet("이벤트스크립트")
sheet_log = client.open("조사 - 이벤트").worksheet("이벤트참여기록")
sheet_inventory = client.open("조사 - 개별(매일 1회)").worksheet("인벤토리")
sheet_rewards = client.open("조사 - 이벤트").worksheet("보상지급여부")

# ========= [이벤트 키워드 정의] ========= #
EVENT_KEYWORDS = {
    "고양이찾기": {"start": "2025-07-16 00:00", "end": "2025-07-16 01:00", "max": 5, "step": 1},
    "책찾기": {"start": "2025-07-16 10:00", "end": "2025-07-16 11:00", "max": 5, "step": 2},
    "부탁들어주기": {"start": "2025-07-16 11:00", "end": "2025-07-16 12:00", "max": 5, "step": 3},
    "쓰레기줍기": {"start": "2025-07-16 19:00", "end": "2025-07-16 20:00", "max": 5, "step": 4},
    "과제보고서": {"start": "2025-07-16 20:00", "end": "2025-07-16 21:00", "max": 5, "step": 5},
    "복숭아따기": {"start": "2025-07-16 21:00", "end": "2025-07-16 22:00", "max": 5, "step": 6},
}

# ========= [유틸 함수] ========= #
def is_within_time(keyword): #제한시간 함수
    now = datetime.now(KST)
    event = EVENT_KEYWORDS[keyword]
    start = KST.localize(datetime.strptime(event['start'], "%Y-%m-%d %H:%M"))
    end = KST.localize(datetime.strptime(event['end'], "%Y-%m-%d %H:%M"))
    return start <= now <= end


def already_participated(user):
    logs = sheet_log.get_all_records()
    for row in logs:
        if row['유저ID'] == user:
            prev_keyword = row['키워드']
            prev_time = f"{row['날짜']} {row['시간']}"
            return True, prev_keyword, prev_time
    return False, None, None

def keyword_usage_count(keyword):
    logs = sheet_log.get_all_records()
    return sum(1 for row in logs if row['키워드'] == keyword)

def previous_step_failed(current_step):
    logs = sheet_log.get_all_records()
    for row in logs:
        print("[DEBUG] row 내용:", row)
        keyword = row.get('키워드')
        step = EVENT_KEYWORDS.get(keyword, {}).get('step')
        result = row.get('종류')
        if step is not None and step < current_step and result == '실패':
            return True
    return False

def get_random_script(keyword):
    scripts = sheet_script.get_all_records()
    candidates = [row for row in scripts if row['키워드'] == keyword]
    if not candidates:
        return "[스크립트 없음]", "실패"
    chosen = random.choice(candidates)
    return chosen['스크립트'], chosen['종류']

def log_participation(user, keyword, result, script):
    now = datetime.now(KST)
    sheet_log.append_row([user, keyword, result, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), script])

def add_gold(user, amount):
    records = sheet_inventory.get_all_records()
    headers = sheet_inventory.row_values(1)
    row_index = next((i+2 for i, r in enumerate(records) if r['유저ID'] == user), None)
    if row_index is None:
        new_row = [''] * len(headers)
        new_row[0] = user
        new_row[headers.index('금')] = str(amount)
        sheet_inventory.append_row(new_row)
        return
    col = headers.index('금') + 1
    current = sheet_inventory.cell(row_index, col).value
    current = int(current) if current and current.isdigit() else 0
    sheet_inventory.update_cell(row_index, col, current + amount)

def reward_given(keyword):
    rewards = sheet_rewards.col_values(1)
    return keyword in rewards

def mark_reward_given(keyword):
    sheet_rewards.append_row([keyword])

def reward_all_users():
    users = sheet_inventory.col_values(1)[1:]  # exclude header
    for user in users:
        if user.strip():
            add_gold(user, 3)

# ========= [작업 큐 및 리스너] ========= #
mention_queue = queue.Queue()

def process_mention(status):
    try:
        content = re.sub('<[^<]+?>', '', status['content'])
        user = status['account']['acct']
        print(f"[DEBUG] 처리 시작: @{user} → {content}")

        for keyword in EVENT_KEYWORDS:
            if f"[{keyword}]" in content:
                event = EVENT_KEYWORDS[keyword]
                print(f"[DEBUG] 감지된 키워드: {keyword}")
                print(f"[DEBUG] 키워드의 step 값: {event.get('step')}")

                if not is_within_time(keyword):
                    masto.status_post(f"@{user} 이 키워드는 지금 사용할 수 없습니다.", in_reply_to_id=status)
                    return

                participated, prev_keyword, prev_time = already_participated(user)
                if participated:
                    masto.status_post(
                        f"@{user} 이미 [{prev_keyword}] 키워드로 {prev_time}에 참여하셨습니다.\n",
                        in_reply_to_id=status
                    )
                    return
                if keyword_usage_count(keyword) >= event['max']:
                    masto.status_post(f"@{user} 인원 제한으로 참여할 수 없습니다.", in_reply_to_id=status)
                    return
                if previous_step_failed(event['step']):
                    masto.status_post(f"@{user} 이전 단계 실패로 진행할 수 없습니다.", in_reply_to_id=status)
                    return

                script, result = get_random_script(keyword)
                log_participation(user, keyword, result, script)

                if result == '성공' and not reward_given(keyword):
                    reward_all_users()
                    mark_reward_given(keyword)

                masto.status_post(f"@{user}\n{script}", in_reply_to_id=status)
                return

    except Exception as e:
        print(f"[ERROR] 처리 오류: {e}")

def worker():
    while True:
        status = mention_queue.get()
        if status is None:
            break
        process_mention(status)
        mention_queue.task_done()

class EventListener(StreamListener):
    def on_notification(self, notification):
        if notification['type'] == 'mention':
            mention_queue.put(notification['status'])

# ========= [봇 실행] ========= #
if __name__ == '__main__':
    me = masto.account_verify_credentials()
    print(f"[INFO] 로그인된 계정: @{me['acct']}")

    for _ in range(5):
        threading.Thread(target=worker, daemon=True).start()

    masto.stream_user(EventListener())