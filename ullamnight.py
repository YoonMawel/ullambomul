# -*- coding: utf-8 -*-
import re
import random
import threading
import queue
from datetime import datetime
from mastodon import Mastodon, StreamListener
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz

# ========= [기본 설정] ========= #
MASTODON_ACCESS_TOKEN = "ATSVp838ZWzZ5poP0xmReBPm7lzrb05bWwAYsDQliC4"
MASTODON_API_BASE_URL = "https://ullambana.xyz"
KST = pytz.timezone("Asia/Seoul")

masto = Mastodon(
    access_token=MASTODON_ACCESS_TOKEN,
    api_base_url=MASTODON_API_BASE_URL
)

# ========= [구글 시트 연동] ========= #
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

sheet_script = client.open("조사 - 이벤트").worksheet("이벤트스크립트")
sheet_log = client.open("조사 - 이벤트").worksheet("이벤트참여기록")
sheet_inventory = client.open("조사 - 개별(매일 1회)").worksheet("인벤토리")

# ========= [이벤트 설정] ========= #
EVENT_KEYWORDS = {
    "금붕어잡기": {"max": 3},
    "경품추첨": {"max": 1},
    "해태잡자": {"max": 3}
}

# ========= [참여 횟수 체크] ========= #
def user_usage_count(user, keyword):
    logs = sheet_log.get_all_records()
    return sum(1 for row in logs if row['유저ID'] == user and row['키워드'] == keyword)

# ========= [스크립트 추출 + 보상 처리] ========= #
def get_random_script(keyword):
    scripts = sheet_script.get_all_records()
    candidates = [row for row in scripts if row['키워드'] == keyword]
    if not candidates:
        return "[스크립트 없음]"
    chosen = random.choice(candidates)
    return chosen['스크립트']

def update_inventory(user, reward_text):
    # reward_text는 "금 30개" 또는 "아이템명 N개" 같은 형식

    if not reward_text or reward_text.strip() == "":
        return  # 보상 없으면 스킵

    # 금인지 아이템인지 확인
    match = re.match(r"(.+)\s+(\d+)개", reward_text.strip())
    if not match:
        return

    name, qty = match.group(1), int(match.group(2))
    if name == "금":
        add_gold(user, qty)
    else:
        add_item(user, name, qty)

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

def add_item(user, item_name, qty):
    records = sheet_inventory.get_all_records()
    headers = sheet_inventory.row_values(1)

    # 아이템 열 없으면 추가
    if item_name not in headers:
        sheet_inventory.add_cols(1)
        sheet_inventory.update_cell(1, len(headers) + 1, item_name)
        headers.append(item_name)

    col = headers.index(item_name) + 1

    # 유저 행 없으면 새로 추가
    row_index = next((i+2 for i, r in enumerate(records) if r['유저ID'] == user), None)
    if row_index is None:
        new_row = [''] * len(headers)
        new_row[0] = user
        new_row[col-1] = str(qty)
        sheet_inventory.append_row(new_row)
        return

    current = sheet_inventory.cell(row_index, col).value
    current = int(current) if current and current.isdigit() else 0
    sheet_inventory.update_cell(row_index, col, current + qty)

# ========= [참여 기록] ========= #
def log_participation(user, keyword, script):
    now = datetime.now(KST)
    sheet_log.append_row([user, keyword, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), script])


mention_queue = queue.Queue()      # 멘션 처리용
writer_queue = queue.Queue()       # 시트 갱신 전용

# ========= [멘션 처리] ========= #

def process_mention(status):
    try:
        content = re.sub('<[^<]+?>', '', status['content'])
        user = status['account']['acct']

        for keyword in EVENT_KEYWORDS:
            if f"[{keyword}]" in content:
                if user_usage_count(user, keyword) >= EVENT_KEYWORDS[keyword]['max']:
                    masto.status_post(f"@{user} [{keyword}] 최대 사용 횟수를 초과했습니다.", in_reply_to_id=status)
                    return

                # 스크립트 + 보상 가져오기
                scripts = sheet_script.get_all_records()
                candidates = [row for row in scripts if row['키워드'] == keyword]
                if not candidates:
                    masto.status_post(f"@{user} 스크립트가 없습니다.", in_reply_to_id=status)
                    return

                chosen = random.choice(candidates)
                script = chosen['스크립트']
                reward = chosen.get('보상1', '')

                log_participation(user, keyword, script)

                # 보상이 있으면 writer_queue에 넣어 시트 갱신
                writer_queue.put((user, reward))

                # 유저에게 출력 (보상 내용은 별도 안내 없이 스크립트만)
                masto.status_post(f"@{user}\n{script}", in_reply_to_id=status)
                return
    except Exception as e:
        print(f"[ERROR] 처리 오류: {e}")


# ========= [워커/리스너] ========= #
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

# ========= [시트 갱신 전용 스레드] ========= #
def writer():
    while True:
        job = writer_queue.get()
        if job is None:
            break
        user, script = job
        update_inventory(user, script)  # 금/아이템 갱신 (한 스레드에서만 실행)
        writer_queue.task_done()


# ========= [봇 실행] ========= #
if __name__ == '__main__':
    me = masto.account_verify_credentials()
    print(f"[INFO] 로그인된 계정: @{me['acct']}")

    # 멘션 처리 워커 (7개)
    for _ in range(6):
        threading.Thread(target=worker, daemon=True).start()

    # 시트 갱신 전용 스레드 (1개)
    threading.Thread(target=writer, daemon=True).start()

    masto.stream_user(EventListener())
