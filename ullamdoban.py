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
from concurrent.futures import ThreadPoolExecutor

# ========= [기본 설정] ========= #
MASTODON_ACCESS_TOKEN = "ACv0ETZ0r43uMyf1bPKqCkEpNjiuU3HOyPTiBbr7c-E"
MASTODON_API_BASE_URL = "https://ullambana.xyz"

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
sheet_partner = client.open("조사 - 이벤트").worksheet("일일도반")
sheet_help = client.open("조사 - 이벤트").worksheet("도움횟수추적")
sheet_inventory = client.open("조사 - 개별(매일 1회)").worksheet("인벤토리")

# ========= [이벤트 키워드 정의] ========= #
EVENT_KEYWORDS = {
    "텃밭전투": {"group": "재료찾기", "period": "2025-07-08 오전", "max": 7},
    "결계보수": {"group": "재료찾기", "period": "2025-07-08 오전", "max": 7},
    "소문듣기": {"group": "재료찾기", "period": "2025-07-08 오전", "max": 7},
    "고기재우기": {"group": "재료손질", "period": "2025-07-08 오후", "max": 6},
    "야채손질": {"group": "재료손질", "period": "2025-07-08 오후", "max": 6},
    "야채볶기": {"group": "요리", "period": "2025-07-09 오전", "max": 6},
    "고기볶기": {"group": "요리", "period": "2025-07-09 오전", "max": 6},
    "카레가루": {"group": "요리", "period": "2025-07-09 오전", "max": 6},
    "플레이팅": {"group": "요리", "period": "2025-07-09 오전", "max": 6},
    "텃밭전투도움": {"group": "재료찾기", "period": "2025-07-08 오전", "max": None},
    "결계보수도움": {"group": "재료찾기", "period": "2025-07-08 오전", "max": None},
    "소문듣기도움": {"group": "재료찾기", "period": "2025-07-08 오전", "max": None},
    "고기재우기도움": {"group": "재료손질", "period": "2025-07-08 오후", "max": None},
    "야채손질도움": {"group": "재료손질", "period": "2025-07-08 오후", "max": None},
    "야채볶기도움": {"group": "요리", "period": "2025-07-09 오전", "max": None},
    "고기볶기도움": {"group": "요리", "period": "2025-07-09 오전", "max": None},
    "카레가루도움": {"group": "요리", "period": "2025-07-09 오전", "max": None},
    "플레이팅도움": {"group": "요리", "period": "2025-07-09 오전", "max": None},
}

# ========= [유틸 함수들] ========= #
def is_help_keyword(keyword):
    return keyword.endswith("도움")

def get_current_period():
    now = datetime.now()
    if now.date() == datetime(2025, 7, 8).date():
        return "2025-07-08 오전" if now.hour < 12 else "2025-07-08 오후"
    elif now.date() == datetime(2025, 7, 9).date():
        return "2025-07-09 오전"
    return None

def already_participated(user, group):
    logs = sheet_log.get_all_records()
    for row in logs:
        if row['유저ID'] == user:
            if EVENT_KEYWORDS.get(row['키워드'], {}).get("group") == group:
                return True
    return False

def count_keyword_usage(keyword):
    logs = sheet_log.get_all_records()
    return sum(1 for row in logs if row['키워드'] == keyword)

def get_random_script(keyword):
    all_scripts = sheet_script.get_all_records()
    candidates = [row for row in all_scripts if row['키워드'] == keyword]
    if not candidates:
        return "[스크립트 없음]", None, None
    choice = random.choice(candidates)
    return choice['스크립트'], choice.get('유형'), choice.get('키워드')

def log_participation(user, keyword, kind, content):
    now = datetime.now()
    sheet_log.append_row([user, keyword, kind, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), content])

def update_inventory(user, item):
    try:
        sheet = sheet_inventory
        headers = sheet.row_values(1)
        records = sheet.get_all_records()
        user_row = None
        for idx, row in enumerate(records):
            if row['유저ID'] == user:
                user_row = idx + 2
                break
        if user_row is None:
            # 유저 행이 없으면 새로 추가
            new_row = [''] * len(headers)
            new_row[0] = user
            new_row[headers.index('소지품')] = f"{item}x1개"
            sheet.append_row(new_row)
            print(f"[DEBUG] 새로운 유저 {user}의 소지품 추가됨: {item}x1개")
            return

        # 소지품 열 인덱스
        col = headers.index('소지품') + 1
        val = sheet.cell(user_row, col).value

        # 기존 내용에 추가
        if not val or val.strip() == "-" or val.strip() == "":
            new_val = f"{item}x1개"
        else:
            new_val = val.strip() + f", {item}x1개"

        sheet.update_cell(user_row, col, new_val)
        print(f"[DEBUG] {user} 소지품 업데이트: {new_val}")
    except Exception as e:
        print(f"[ERROR] 인벤토리 갱신 실패: {e}")

def get_user_items(user):
    all_users = sheet_inventory.col_values(1)
    try:
        row_index = all_users.index(user) + 1
    except ValueError:
        sheet_inventory.append_row([user, 0, 0, "-"])
        return {}, len(all_users) + 1

    row = sheet_inventory.row_values(row_index)
    raw = row[3] if len(row) > 3 else "-"
    items = {}

    if raw != "-" and raw.strip():
        for token in raw.split(","):
            match = re.match(r"\s*(.+?)x(\d+)개\s*", token.strip())
            if match:
                name, count = match.group(1).strip(), int(match.group(2))
                items[name] = count

    return items, row_index

def write_user_items(user, items, row_index):
    if not items:
        item_str = "-"
    else:
        item_str = ", ".join([f"{k}x{v}개" for k, v in items.items()])
    sheet_inventory.update_cell(row_index, 4, item_str)

def add_item(user, item_name):
    items, row_index = get_user_items(user)
    items[item_name] = items.get(item_name, 0) + 1
    write_user_items(user, items, row_index)

def check_help_limit(user, target):
    records = sheet_help.get_all_records()
    row_idx = None
    for i, row in enumerate(records):
        if row['유저ID'] == user:
            row_idx = i + 2
            break
    if row_idx is None:
        sheet_help.append_row([user, 0, 0])
        row_idx = len(records) + 2
    data = sheet_help.row_values(row_idx)
    doban_count = int(data[1]) if data[1] else 0
    normal_count = int(data[2]) if data[2] else 0
    if user == target:
        sheet_help.update_cell(row_idx, 2, doban_count + 1)
        return True
    else:
        if normal_count >= 3:
            return False
        sheet_help.update_cell(row_idx, 3, normal_count + 1)
        return True

def get_doban(user):
    records = sheet_partner.get_all_records()
    for row in records:
        if row['유저ID'] == user:
            return row['도반ID']
    return None

def count_user_general_participation(user):
    logs = sheet_log.get_all_records()
    return sum(1 for row in logs if row['유저ID'] == user and row['종류'] == '일반')

def already_used_keyword(user, keyword):
    logs = sheet_log.get_all_records()
    return any(row['유저ID'] == user and row['키워드'] == keyword for row in logs)

# ========= [작업 큐 및 워커] ========= #
mention_queue = queue.Queue()

def process_mention(status):
    try:
        content = re.sub('<[^<]+?>', '', status["content"])
        user = status["account"]["acct"]
        print(f"[DEBUG] 처리 시작 → 유저: @{user}, 내용: {content}")
        for keyword in EVENT_KEYWORDS:
            if f"[{keyword}]" in content:
                print(f"[DEBUG] 유저: @{user}, 키워드: {keyword}")
                period = get_current_period()
                if period != EVENT_KEYWORDS[keyword]['period']:
                    masto.status_post(f"@{user} 해당 키워드는 현재 시간에 사용할 수 없습니다.", in_reply_to_id=status)
                    return
                if not is_help_keyword(keyword):
                    if already_used_keyword(user, keyword):
                        masto.status_post(f"@{user} 이미 해당 키워드에 참여했습니다. 다른 키워드를 사용해주세요.", in_reply_to_id=status)
                        return
                    if count_user_general_participation(user) >= 3:
                        masto.status_post(f"@{user} 일반 키워드는 총 3회까지만 참여할 수 있습니다.", in_reply_to_id=status)
                        return
                    if EVENT_KEYWORDS[keyword]['max']:
                        if count_keyword_usage(keyword) >= EVENT_KEYWORDS[keyword]['max']:
                            masto.status_post(f"@{user} 해당 키워드는 인원 제한으로 마감되었습니다.", in_reply_to_id=status)
                            return
                if is_help_keyword(keyword):
                    mentions = [
                        acct["acct"] for acct in status["mentions"]
                        if acct["acct"] != user and acct["acct"] != "autobot1"  # 자동봇 제외
                    ]
                    if not mentions:
                        masto.status_post(f"@{user} 도움 키워드는 대상 유저를 멘션해야 합니다.", in_reply_to_id=status)
                        return
                    target = mentions[0]
                    doban = get_doban(user)

                    #도반이 아닌 경우에만 횟수 제한 체크
                    if target != doban:
                        if not check_help_limit(user, target):
                            masto.status_post(f"@{user} 일반 유저에 대한 도움은 최대 3회까지만 가능합니다.", in_reply_to_id=status)
                            return

                script, script_type, _ = get_random_script(keyword)
                log_participation(user, keyword, "도움" if is_help_keyword(keyword) else "일반", script)
                if script_type == "보상":
                    match = re.search(r"\[(.+?)\][을를] 획득", script)
                    if match:
                        add_item(user, f"{match.group(1)}")
                masto.status_post(f"@{user}\n{script}", in_reply_to_id=status)
                return

    except Exception as e:
        user = status["account"]["acct"]
        print(f"[ERROR] 유저: @{user} 처리 중 오류: {e}")

def worker():
    while True:
        status = mention_queue.get()
        if status is None:
            break
        process_mention(status)
        mention_queue.task_done()

# ========= [마스토돈 리스너] ========= #
class EventListener(StreamListener):
    def on_notification(self, notification):
        if notification["type"] == "mention":
            status = notification["status"]
            user = status["account"]["acct"]
            content = re.sub('<[^<]+?>', '', status["content"])
            print(f"[DEBUG] 멘션 수신: @{user} → {content}")
            mention_queue.put(status)

# ========= [봇 실행] ========= #
if __name__ == '__main__':
    me = masto.account_verify_credentials()
    print(f"[INFO] 로그인된 마스토돈 봇 계정: @{me['acct']}")
    print("[INFO] 마스토돈 도반봇 실행 중...")

    # 병렬 워커 스레드 실행
    for _ in range(9):
        threading.Thread(target=worker, daemon=True).start()

    listener = EventListener()
    masto.stream_user(listener)