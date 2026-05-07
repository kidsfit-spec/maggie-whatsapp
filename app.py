"""
Maggie WhatsApp 溝通系統 v2.7.0
KIDS FIT

對話狀態機：
  大王（85268993194）：
    IDLE → 發訊息（含目標號碼）→ Maggie 改寫 → AWAITING_CONFIRM
    AWAITING_CONFIRM → 確認 → 生成語音 → 直接發到目標號碼 + 副本給大王 → IDLE
    AWAITING_CONFIRM → 確認（無目標號碼）→ AWAITING_NUMBER → 收到號碼 → 發送 → IDLE

  85263951689：
    IDLE → 發訊息 → Maggie 改寫 → AWAITING_CONFIRM
    AWAITING_CONFIRM → 確認 → 生成語音 → 發回本人 → IDLE

  非白名單：
    發任何訊息 → 轉發給大王（不回覆對方）

v2.7.0 修正：
  - 使用文件持久化狀態（/tmp/maggie_states.json），防止進程重啟丟失
  - AWAITING_NUMBER 狀態下支持多號碼（「93365596 同 95068886」）
  - 加入 threading.Lock 確保狀態讀寫線程安全
"""

import os
import json
import logging
import re
import threading
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ─── 日誌設定 ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── 憑證與配置 ───────────────────────────────────────────────────────────────

WA_PHONE_NUMBER_ID = "780503051810575"
WA_ACCESS_TOKEN = (
    "EAAdmmySD1n8BReIPTLEoC6419ZATVKuT7IEopr8yJGB52ywYGisjHEa4ghiNESbXmAlDPZBx"
    "DiarYzpLrjCSA00Rb9KpF2drAiaNr0IzKWmceqv590OUazCqjL9KQgi7yAkn2N0dRvDXjZAew"
    "ObodPH2ppegvKl9AiN702f5D0SfixeJHsnylBOisT0VwZDZD"
)
WA_VERIFY_TOKEN = "kidsfit_maggie_2024"
WA_API_BASE = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}"

MINIMAX_API_KEY = (
    "sk-api-3hXH9X2CMVPLfIIsWCGVoqcG4XQXF83T1jus6_UPu4DDW-jy4-1ctvb1a44X_-"
    "mURow51iEiV3VbNGQxMh7Pw3qjrUbIAdNTTyKJITjFhlCbaw2GxwSAQy0"
)
MINIMAX_VOICE_ID = "Cantonese_casual_narrator_vv2"
MINIMAX_MODEL = "speech-2.8-hd"
MINIMAX_ENDPOINT = "https://api.minimax.io/v1/t2a_v2"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 完整功能白名單
WHITELIST = {"85268993194", "85263951689"}

# 大王號碼（接收第三方轉發通知）
DAWANG_NUMBER = "85268993194"

# Google Calendar 設定
GCAL_CALENDAR_ID = "info@kidsfit.com.hk"
GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"

# 教練 WhatsApp 號碼對照表
COACH_NUMBERS = {
    "可樂": "85262834191",
    "倉鼠": "85264222428",
    "西瓜": "85265350841",
    "樹熊": "85251384906",
}
ALL_COACH_NUMBERS = list(COACH_NUMBERS.values())

HK_TZ = pytz.timezone("Asia/Hong_Kong")

# ─── 狀態管理（文件持久化 + 線程安全）──────────────────────────────────────────

STATE_IDLE = "idle"
STATE_AWAITING_CONFIRM = "awaiting_confirm"
STATE_AWAITING_NUMBER = "awaiting_number"

STATE_FILE = "/tmp/maggie_states.json"
HISTORY_FILE = "/tmp/maggie_history.json"
_state_lock = threading.Lock()

def _load_states() -> dict:
    """從文件載入所有用戶狀態"""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[STATE LOAD ERROR] {e}")
    return {}

def _save_states(states: dict):
    """將所有用戶狀態保存到文件"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(states, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[STATE SAVE ERROR] {e}")

def _load_history() -> dict:
    """從文件載入對話歷史"""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[HISTORY LOAD ERROR] {e}")
    return {}

def _save_history(history: dict):
    """將對話歷史保存到文件"""
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[HISTORY SAVE ERROR] {e}")

def get_user_state(user_id: str) -> dict:
    """獲取用戶狀態（線程安全）"""
    with _state_lock:
        states = _load_states()
        if user_id not in states:
            states[user_id] = {
                "state": STATE_IDLE,
                "rewritten_text": "",
                "target_numbers": [],
            }
            _save_states(states)
        return states[user_id]

def set_user_state(user_id: str, state_data: dict):
    """設定用戶狀態並持久化（線程安全）"""
    with _state_lock:
        states = _load_states()
        states[user_id] = state_data
        _save_states(states)
    logger.info(f"[STATE SET] user={user_id} state={state_data.get('state')} targets={state_data.get('target_numbers', [])}")

def reset_user_state(user_id: str):
    """重置用戶狀態（線程安全）"""
    with _state_lock:
        states = _load_states()
        states[user_id] = {
            "state": STATE_IDLE,
            "rewritten_text": "",
            "target_numbers": [],
        }
        _save_states(states)
    logger.info(f"[STATE RESET] user={user_id}")

def get_history(user_id: str) -> list:
    history = _load_history()
    return history.get(user_id, [])

def add_to_history(user_id: str, role: str, content: str):
    history = _load_history()
    if user_id not in history:
        history[user_id] = []
    history[user_id].append({"role": role, "content": content})
    if len(history[user_id]) > 8:
        history[user_id] = history[user_id][-8:]
    _save_history(history)


# ─── AI 系統提示 ──────────────────────────────────────────────────────────────

MAGGIE_SYSTEM_PROMPT = """你係 Maggie，KIDS FIT 嘅行政人員，亦係大王（Arts Mak）嘅私人助理。

你嘅背景：
- KIDS FIT 主要客戶係香港幼稚園，客戶大多數係女性，包括校長、主任同老師
- 大王係一位比較率直嘅男士，以最簡單快捷方法解決問題為主
- 大王經常同客戶唔能夠有效溝通，所以需要你幫佢學習及探討點樣友善咁向客戶表達意見
- 你同大王用純正香港粵語傾偈，簡單直接，唔做作

你嘅任務：
將大王想表達嘅內容，改寫為友善自然嘅廣東話語音稿，Maggie 會直接用 WhatsApp Business 號碼發送語音給對方。

最重要嘅原則（必須嚴格遵守）：
- 語音稿必須用第一人稱「我」說話，因為係代表大王本人發言
- 絕對唔可以出現「Arts」「Arts Mak」「大王」呢類第三人稱
- 直接以「我」嘅身份開口說話
- 但係同大王對話時，要稱呼佢做「大王」

改寫原則：
1. 保留原意，唔改變核心訊息
2. 語氣友善但唔諂媚，真誠唔虛偽，像朋友之間講嘢咁自然
3. 符合香港商業溝通習慣，用詞得體
4. 廣東話口語風格，自然流暢，適合朗讀
5. 長度適中，唔好過長
6. 唔使用 Emoji

正確示範（語音稿）：
- 「我記得上次都有提過...」（正確）
- 「我哋 KIDS FIT 最近有個新課程...」（正確）
- 「我想約個時間同你傾下...」（正確）

錯誤示範（絕對禁止）：
- 「Arts 記得上次都有提過...」（錯誤，第三人稱）
- 「大王想話...」（錯誤，第三人稱）

回覆格式要求（非常重要）：
- 你的回覆必須分為兩部分，用 "---" 分隔
- 第一部分：純粹的語音稿內容（將會被 TTS 朗讀的文字，不要加任何標記或引號）
- 第二部分：給大王的確認訊息（用「大王」稱呼，香港粵語，簡單直接）
- 範例格式：

校長你好，我係 KIDS FIT 嘅 Arts。我哋最近有個新嘅體能課程方案，想睇下貴校有冇興趣了解一下。方便嘅話我哋可以約個時間傾下。
---
大王，以上係改寫後嘅語音稿。OK嘅話回覆「OK」或「好」，我即刻幫你生成語音直接發出去。唔啱可以回覆「取消」。

重要：
- 語音稿部分要適合廣東話朗讀，自然流暢
- 同大王對話時用香港粵語，簡單直接
- 唔使用 Emoji
"""

# ─── WhatsApp API 工具函數 ────────────────────────────────────────────────────

def send_whatsapp_text(to: str, message: str) -> dict:
    """發送文字訊息"""
    url = f"{WA_API_BASE}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    logger.info(f"[WA TEXT] to={to} status={resp.status_code}")
    return resp.json()


def download_whatsapp_media(media_id: str) -> bytes:
    """下載 WhatsApp 媒體檔案"""
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    media_url = resp.json().get("url")
    resp2 = requests.get(media_url, headers=headers, timeout=60)
    resp2.raise_for_status()
    logger.info(f"[WA MEDIA] downloaded {len(resp2.content)} bytes")
    return resp2.content


def upload_whatsapp_audio(audio_bytes: bytes) -> str:
    """上傳音頻到 WhatsApp，返回 media_id"""
    url = f"{WA_API_BASE}/media"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}
    files = {
        "file": ("audio.mp3", audio_bytes, "audio/mpeg"),
        "messaging_product": (None, "whatsapp"),
        "type": (None, "audio/mpeg")
    }
    resp = requests.post(url, headers=headers, files=files, timeout=60)
    logger.info(f"[WA UPLOAD] status={resp.status_code} body={resp.text[:200]}")
    resp.raise_for_status()
    return resp.json().get("id")


def send_whatsapp_audio(to: str, media_id: str) -> dict:
    """發送語音訊息"""
    url = f"{WA_API_BASE}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"id": media_id}
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    logger.info(f"[WA AUDIO] to={to} status={resp.status_code} resp={resp.text[:200]}")
    return resp.json()


def format_phone_display(number: str) -> str:
    """格式化電話號碼顯示，例如 85268993194 → +852 6899 3194"""
    n = number.lstrip("+")
    if n.startswith("852") and len(n) == 11:
        local = n[3:]
        return f"+852 {local[:4]} {local[4:]}"
    return f"+{n}"


# ─── STT（Gemini 多模態語音識別）──────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """使用 Gemini 多模態 API 將語音轉為文字"""
    import base64

    mime_map = {
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
    }
    mime_type = mime_map.get(suffix.lower(), "audio/ogg")
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    payload = {
        "contents": [{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": audio_b64
                    }
                },
                {
                    "text": "請將這段廣東話語音轉錄為文字，只輸出文字內容，不要加任何解釋或標點以外的內容。"
                }
            ]
        }],
        "generationConfig": {"temperature": 0}
    }

    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    logger.info(f"[STT Gemini] 識別結果: {text[:100]}")
    return text


# ─── Gemini AI 改寫 ───────────────────────────────────────────────────────────

def call_gemini(user_message: str, user_id: str) -> str:
    """呼叫 Gemini 生成改寫回覆"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    history = get_history(user_id)

    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(
            role=role,
            parts=[types.Part(text=msg["content"])]
        ))
    contents.append(types.Content(
        role="user",
        parts=[types.Part(text=user_message)]
    ))

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=MAGGIE_SYSTEM_PROMPT,
            temperature=0.7,
        ),
        contents=contents
    )
    return response.text


# ─── MiniMax TTS ─────────────────────────────────────────────────────────────

def text_to_speech(text: str) -> bytes:
    """使用 MiniMax TTS 生成廣東話語音，返回 MP3 bytes"""
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MINIMAX_MODEL,
        "text": text,
        "voice_setting": {
            "voice_id": MINIMAX_VOICE_ID,
            "speed": 0.9,
            "vol": 1.0,
            "pitch": 0
        },
        "audio_setting": {
            "format": "mp3",
            "sample_rate": 32000,
            "bitrate": 128000
        },
        "language_boost": "Chinese,Yue"
    }
    resp = requests.post(MINIMAX_ENDPOINT, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    audio_hex = data.get("data", {}).get("audio", "")
    if not audio_hex:
        raise ValueError(f"MiniMax TTS 返回空音頻: {json.dumps(data)[:300]}")

    audio_bytes = bytes.fromhex(audio_hex)
    logger.info(f"[TTS] 生成音頻 {len(audio_bytes)} bytes")
    return audio_bytes


# ─── 工具函數 ─────────────────────────────────────────────────────────────────

def is_confirmation(text: str) -> bool:
    """判斷是否為確認詞"""
    text_lower = text.lower().strip()
    exact_confirmations = {
        "ok", "send", "得", "發", "確認", "yes", "係",
        "好", "發送", "ok la", "得la", "得喇", "ok喇",
        "send喇", "傳送", "go", "go ahead", "傳",
        "可以", "okay", "收到", "好的", "好啊", "好呀",
        "ok啦", "得啦", "好啦", "可以啦"
    }
    if text_lower in exact_confirmations:
        return True
    long_confirmations = ["ok喇", "send喇", "得喇", "ok la", "go ahead", "確認發送", "可以發", "ok啦", "得啦"]
    return any(conf in text_lower for conf in long_confirmations)


def is_cancel(text: str) -> bool:
    """判斷是否為取消詞"""
    text_lower = text.lower().strip()
    cancels = {"唔好", "取消", "cancel", "算", "唔使", "唔洗", "no", "唔要", "重嚟", "重來"}
    if text_lower in cancels:
        return True
    cancel_phrases = ["唔好意思", "算啦", "唔使喇", "取消發送"]
    return any(p in text_lower for p in cancel_phrases)


def extract_phone_number(text: str) -> str:
    """
    從文字中提取單個香港電話號碼。
    支援格式：85212345678、+85212345678、12345678（8位本地號碼）
    返回標準格式（帶852前綴），找不到則返回空字串。
    """
    # 先嘗試匹配帶852前綴的11位號碼
    m = re.search(r'(?:\+?852)([235689]\d{7})', text)
    if m:
        return "852" + m.group(1)

    # 再嘗試匹配純8位香港號碼（2/3/5/6/9開頭）
    m = re.search(r'(?:^|[\s\u4e00-\u9fff：:，,])([235689]\d{7})(?:[\s\u4e00-\u9fff：:,，。]|$)', text)
    if m:
        return "852" + m.group(1)

    return ""


def extract_multiple_phone_numbers(text: str) -> list:
    """
    從文字中提取多個香港電話號碼。
    支援格式：「93365596 同 95068886」「93365596, 95068886」「93365596、95068886」
    返回標準格式列表（帶852前綴）。
    """
    numbers = []

    # 匹配帶852前綴的號碼
    for m in re.finditer(r'(?:\+?852)([235689]\d{7})', text):
        num = "852" + m.group(1)
        if num not in numbers:
            numbers.append(num)

    # 匹配純8位號碼（2/3/5/6/9開頭）- 用 lookahead/lookbehind 避免消耗分隔符
    for m in re.finditer(r'(?:^|(?<=\D))([235689]\d{7})(?=\D|$)', text):
        num = "852" + m.group(1)
        if num not in numbers:
            numbers.append(num)

    # 如果上面沒找到，嘗試更寬鬆的匹配（純數字序列）
    if not numbers:
        # 清理文字，找所有8位或11位數字序列
        clean = re.sub(r'[同和,，、\s\+]', ' ', text)
        for part in clean.split():
            part = part.strip()
            if re.fullmatch(r'852[235689]\d{7}', part):
                num = part
                if num not in numbers:
                    numbers.append(num)
            elif re.fullmatch(r'[235689]\d{7}', part):
                num = "852" + part
                if num not in numbers:
                    numbers.append(num)

    return numbers


def contains_phone_numbers(text: str) -> bool:
    """判斷文字中是否包含電話號碼"""
    return len(extract_multiple_phone_numbers(text)) > 0


def parse_gemini_response(response: str) -> tuple:
    """
    解析 Gemini 回覆，分離語音稿和確認訊息
    返回 (voice_script, full_reply_to_user)
    """
    if "---" in response:
        parts = response.split("---", 1)
        voice_script = parts[0].strip()
        if voice_script and len(voice_script) > 10:
            return voice_script, response

    # 備用：嘗試提取引號內容
    quote_patterns = [r'「([\s\S]+?)」', r'"([\s\S]+?)"']
    for pattern in quote_patterns:
        matches = re.findall(pattern, response)
        if matches:
            longest = max(matches, key=len)
            if len(longest) > 15:
                return longest.strip(), response

    # 最後手段：取第一段
    paragraphs = [p.strip() for p in response.split('\n\n') if p.strip()]
    for para in paragraphs:
        if len(para) > 15 and not any(kw in para for kw in ["確認", "請問", "是否", "OK", "Send", "回覆"]):
            return para, response

    return response.strip(), response


# ─── 第三方訊息轉發 ───────────────────────────────────────────────────────────

def forward_third_party_message(from_number: str, msg_type: str, msg_content: dict):
    """將非白名單用戶的訊息轉發給大王，不回覆對方"""
    display = format_phone_display(from_number)

    if msg_type == "text":
        body = msg_content.get("body", "").strip()
        notify = f"[{display} 回覆]：{body}"
        send_whatsapp_text(DAWANG_NUMBER, notify)
        logger.info(f"[FORWARD TEXT] from={from_number} to={DAWANG_NUMBER}")

    elif msg_type == "audio":
        audio_id = msg_content.get("id")
        if audio_id:
            try:
                audio_bytes = download_whatsapp_media(audio_id)
                transcribed = transcribe_audio(audio_bytes)
                notify = f"[{display} 語音回覆]：{transcribed}"
            except Exception as e:
                logger.error(f"[FORWARD AUDIO STT ERROR] {e}")
                notify = f"[{display} 語音回覆]：（語音識別失敗，請查看原始語音）"
            send_whatsapp_text(DAWANG_NUMBER, notify)
        else:
            send_whatsapp_text(DAWANG_NUMBER, f"[{display} 語音回覆]：（無法下載語音）")
        logger.info(f"[FORWARD AUDIO] from={from_number} to={DAWANG_NUMBER}")

    elif msg_type == "image":
        send_whatsapp_text(DAWANG_NUMBER, f"[{display} 發送了一張圖片]")
        logger.info(f"[FORWARD IMAGE] from={from_number}")

    elif msg_type == "document":
        send_whatsapp_text(DAWANG_NUMBER, f"[{display} 發送了一個檔案]")
        logger.info(f"[FORWARD DOC] from={from_number}")

    elif msg_type == "video":
        send_whatsapp_text(DAWANG_NUMBER, f"[{display} 發送了一段影片]")
        logger.info(f"[FORWARD VIDEO] from={from_number}")

    else:
        send_whatsapp_text(DAWANG_NUMBER, f"[{display} 發送了一條訊息（類型：{msg_type}）]")
        logger.info(f"[FORWARD OTHER] from={from_number} type={msg_type}")


# ─── 主要訊息處理邏輯（狀態機）────────────────────────────────────────────────

def process_message(from_number: str, msg_type: str, msg_content: dict):
    """處理接收到的 WhatsApp 訊息"""

    # 非白名單：轉發給大王，不回覆對方
    if from_number not in WHITELIST:
        logger.info(f"[THIRD PARTY] 轉發訊息: from={from_number} type={msg_type}")
        forward_third_party_message(from_number, msg_type, msg_content)
        return

    # 白名單用戶：走完整 Maggie 流程
    # 取得用戶文字內容
    text = ""
    if msg_type == "text":
        text = msg_content.get("body", "").strip()
    elif msg_type == "audio":
        audio_id = msg_content.get("id")
        if not audio_id:
            send_whatsapp_text(from_number, "無法讀取語音訊息，請重試。")
            return
        send_whatsapp_text(from_number, "收到語音訊息，正在識別中...")
        try:
            audio_bytes = download_whatsapp_media(audio_id)
            text = transcribe_audio(audio_bytes)
            if not text.strip():
                send_whatsapp_text(from_number, "無法識別語音內容，請重試或改用文字。")
                return
            send_whatsapp_text(from_number, f"識別內容：{text}")
        except Exception as e:
            logger.error(f"[STT ERROR] {e}", exc_info=True)
            send_whatsapp_text(from_number, f"語音識別失敗：{str(e)[:100]}")
            return
    else:
        send_whatsapp_text(from_number, "Maggie 目前支援文字和語音訊息。")
        return

    if not text:
        return

    # *** 關鍵：先讀取狀態，然後根據狀態決定處理方式 ***
    state = get_user_state(from_number)
    current_state = state["state"]
    logger.info(f"[STATE CHECK] user={from_number} state={current_state} input='{text[:80]}'")

    try:
        # ═══════════════════════════════════════════════════════════════════
        # 狀態：等待目標電話號碼（AWAITING_NUMBER）
        # ═══════════════════════════════════════════════════════════════════
        if current_state == STATE_AWAITING_NUMBER:
            logger.info(f"[AWAITING_NUMBER] 收到: '{text}'")

            if is_cancel(text):
                reset_user_state(from_number)
                send_whatsapp_text(from_number, "已取消。有新訊息隨時再發給我。")
                return

            # 嘗試提取多個號碼
            phones = extract_multiple_phone_numbers(text)
            if phones:
                logger.info(f"[AWAITING_NUMBER] 提取到號碼: {phones}")
                state["target_numbers"] = phones
                set_user_state(from_number, state)
                # 有號碼，直接執行發送
                _execute_generate_and_send(from_number)
                return

            # 沒有找到號碼
            send_whatsapp_text(from_number, "大王，請輸入有效嘅香港電話號碼（例如：63951689 或 85263951689）。\n\n多個號碼可以用「同」或「,」分隔，例如：93365596 同 95068886")
            return

        # ═══════════════════════════════════════════════════════════════════
        # 狀態：等待用戶確認改寫內容（AWAITING_CONFIRM）
        # ═══════════════════════════════════════════════════════════════════
        if current_state == STATE_AWAITING_CONFIRM:
            logger.info(f"[AWAITING_CONFIRM] 收到: '{text}' | is_confirm={is_confirmation(text)} | is_cancel={is_cancel(text)}")

            if is_cancel(text):
                reset_user_state(from_number)
                send_whatsapp_text(from_number, "已取消。有新訊息隨時再發給我。")
                return

            if is_confirmation(text):
                targets = state.get("target_numbers", [])
                if not targets and from_number == DAWANG_NUMBER:
                    # 大王確認但沒有目標號碼，問號碼
                    state["state"] = STATE_AWAITING_NUMBER
                    set_user_state(from_number, state)
                    send_whatsapp_text(from_number, "大王，想發到邊個號碼？\n（多個號碼可以用「同」或「,」分隔）")
                    return
                # 有目標號碼或非大王（發回本人）
                _execute_generate_and_send(from_number)
                return

            # 不是確認也不是取消 → 當作新的改寫請求
            logger.info(f"[AWAITING_CONFIRM] 非確認非取消，當作新訊息處理")
            reset_user_state(from_number)
            _handle_new_message(from_number, text)
            return

        # ═══════════════════════════════════════════════════════════════════
        # 狀態：空閒（IDLE，接收新訊息）
        # ═══════════════════════════════════════════════════════════════════
        else:  # STATE_IDLE
            _handle_new_message(from_number, text)

    except Exception as e:
        logger.error(f"[ERROR] 處理訊息失敗: {e}", exc_info=True)
        reset_user_state(from_number)
        send_whatsapp_text(from_number, "系統發生錯誤，請稍後再試。")


def _handle_direct_text(from_number: str, text: str) -> bool:
    """
    檢查是否為「文字直接回覆」指令。
    格式：「文字回覆 / 文字 / text」+ 號碼 + 內容
    如果符合，直接發送文字，返回 True；否則返回 False。
    """
    # 匹配觸發關鍵詞
    m = re.match(
        r'^(?:文字回覆|文字|text)\s+(?:\+?852)?([235689]\d{7})\s+(.+)$',
        text.strip(), re.IGNORECASE | re.DOTALL
    )
    if not m:
        return False

    raw_number = m.group(1).strip()
    content = m.group(2).strip()

    # 補上 852 前綴
    if len(raw_number) == 8:
        target = "852" + raw_number
    else:
        target = raw_number

    target_display = format_phone_display(target)
    logger.info(f"[DIRECT TEXT] from={from_number} to={target} content={content[:50]}")

    result = send_whatsapp_text(target, content)
    if "messages" in result:
        send_whatsapp_text(from_number, f"已發送文字到 {target_display}。")
        logger.info(f"[DIRECT TEXT] 發送成功 to={target}")
    else:
        err_code = result.get("error", {}).get("code", 0)
        err_msg = result.get("error", {}).get("message", "未知錯誤")
        is_window_error = err_code in (131047, 131026, 131028) or \
            any(kw in err_msg.lower() for kw in ["24", "window", "session", "outside"])
        if is_window_error:
            send_whatsapp_text(
                from_number,
                f"大王，文字發送到 {target_display} 失敗。"
                f"對方未開啟對話窗口，請叫對方先發一條訊息到 95789829。"
            )
        else:
            send_whatsapp_text(from_number, f"大王，文字發送到 {target_display} 失敗：{err_msg[:80]}")
        logger.error(f"[DIRECT TEXT] 發送失敗 to={target} code={err_code}: {err_msg}")

    return True


def _handle_new_message(from_number: str, text: str):
    """處理新的改寫請求，同時嘗試提取目標號碼"""

    if not text.strip():
        send_whatsapp_text(
            from_number,
            "大王，你想表達咩？話俾我知，我幫你改寫為友善自然嘅版本。"
        )
        return

    # 大王專用：文字直接回覆指令（不經改寫）
    if from_number == DAWANG_NUMBER and _handle_direct_text(from_number, text):
        return

    # 嘗試從訊息中提取目標號碼（大王專用）
    target_numbers = []
    message_content = text
    if from_number == DAWANG_NUMBER:
        target_numbers = extract_multiple_phone_numbers(text)
        if target_numbers:
            # 移除號碼部分，保留訊息內容
            cleaned = re.sub(
                r'(?:幫我)?(?:同|發俾|發給|發到|告訴|通知)\s*(?:\+?852)?\s*\d{8,11}\s*(?:講|說|話|：|:)?\s*',
                '', text, flags=re.IGNORECASE
            ).strip()
            # 移除所有號碼本身
            cleaned = re.sub(r'(?:\+?852)?[235689]\d{7}', '', cleaned).strip()
            # 移除連接詞
            cleaned = re.sub(r'^[\s同和,，、]+|[\s同和,，、]+$', '', cleaned).strip()
            # 如果清理後有內容就用，否則用原文
            if cleaned and len(cleaned) > 2:
                message_content = cleaned
            logger.info(f"[EXTRACT] 目標號碼={target_numbers} 內容={message_content[:50]}")

    # 呼叫 Gemini 改寫
    prompt = f"大王想表達：「{message_content}」\n\n請用第一人稱「我」改寫為友善自然嘅廣東話語音稿。記住：語音稿係代表大王本人講嘅，唔可以出現第三人稱。"
    gemini_reply = call_gemini(prompt, from_number)

    # 解析回覆
    voice_script, full_reply = parse_gemini_response(gemini_reply)

    # 更新對話歷史
    add_to_history(from_number, "user", text)
    add_to_history(from_number, "assistant", gemini_reply)

    # 設定狀態並持久化
    new_state = {
        "state": STATE_AWAITING_CONFIRM,
        "rewritten_text": voice_script,
        "target_numbers": target_numbers,
    }
    set_user_state(from_number, new_state)

    # 回覆用戶
    send_whatsapp_text(from_number, full_reply)


def _execute_generate_and_send(from_number: str):
    """生成語音並發送"""
    state = get_user_state(from_number)
    rewritten_text = state.get("rewritten_text", "")
    target_numbers = state.get("target_numbers", [])

    if not rewritten_text:
        send_whatsapp_text(from_number, "系統錯誤：缺少語音稿。請重新發送訊息。")
        reset_user_state(from_number)
        return

    send_whatsapp_text(from_number, "正在生成語音...")

    try:
        # 生成廣東話語音
        audio_bytes = text_to_speech(rewritten_text)

        # 上傳到 WhatsApp
        media_id = upload_whatsapp_audio(audio_bytes)

        # 決定發送邏輯
        if from_number == DAWANG_NUMBER and target_numbers:
            # 大王有指定目標號碼：直接發到對方 + 副本給大王
            success_targets = []
            failed_targets = []

            for target in target_numbers:
                target_display = format_phone_display(target)
                result = send_whatsapp_audio(target, media_id)
                if "messages" in result:
                    success_targets.append(target_display)
                    logger.info(f"[SEND TO TARGET] to={target} OK")
                else:
                    err_code = result.get("error", {}).get("code", 0)
                    err_msg = result.get("error", {}).get("message", "未知錯誤")
                    failed_targets.append((target_display, err_code, err_msg))
                    logger.error(f"[SEND TO TARGET] to={target} FAILED code={err_code}: {err_msg}")

            # 副本給大王（無論成功失敗都發）
            send_whatsapp_audio(from_number, media_id)
            logger.info(f"[SEND COPY TO DAWANG] to={from_number}")

            # 通知結果
            msg_parts = []
            if success_targets:
                msg_parts.append(f"大王，語音已直接發送到：{', '.join(success_targets)}")
            for (disp, code, err) in failed_targets:
                # 判斷是否 24 小時窗口問題（error code 131047 或 131026）
                is_window_error = code in (131047, 131026, 131028) or \
                    any(kw in err.lower() for kw in ["24", "window", "session", "outside", "re-engagement"])
                if is_window_error:
                    msg_parts.append(
                        f"大王，語音發送到 {disp} 失敗。"
                        f"對方未開啟對話窗口，請叫對方先發一條訊息到 95789829。"
                    )
                else:
                    msg_parts.append(f"大王，語音發送到 {disp} 失敗：{err[:80]}")
            msg_parts.append("副本已發回俾你留底。")
            send_whatsapp_text(from_number, "\n\n".join(msg_parts))

        else:
            # 非大王 或 大王沒有指定目標號碼：發回本人
            send_whatsapp_audio(from_number, media_id)
            send_whatsapp_text(from_number, "語音已生成，發回俾你。")

        # 更新對話歷史
        add_to_history(from_number, "user", "[確認生成語音]")
        add_to_history(from_number, "assistant", "語音已生成並發送。")

    except Exception as e:
        logger.error(f"[TTS/SEND ERROR] {e}", exc_info=True)
        send_whatsapp_text(
            from_number,
            f"語音生成失敗：{str(e)[:150]}\n\n請稍後再試。"
        )
    finally:
        reset_user_state(from_number)


# ─── Flask 路由 ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Maggie WhatsApp 溝通系統",
        "description": "KIDS FIT AI 溝通助手 Maggie",
        "status": "running",
        "version": "2.8.2",
        "flow": {
            "大王": "發訊息（含目標號碼）→ Maggie改寫 → 確認 → 直接發語音到對方 + 副本給大王",
            "85263951689": "發訊息 → Maggie改寫 → 確認 → 語音發回本人",
            "第三方": "發訊息 → 自動轉發給大王（不回覆對方）"
        }
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook/whatsapp", methods=["GET"])
def webhook_verify():
    """WhatsApp Webhook 驗證"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    logger.info(f"[WEBHOOK] 驗證: mode={mode} token={token}")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        logger.info("[WEBHOOK] 驗證成功")
        return challenge, 200
    else:
        return "Forbidden", 403


@app.route("/webhook/whatsapp", methods=["POST"])
def webhook_receive():
    """接收 WhatsApp 訊息"""
    try:
        data = request.get_json(force=True)
        logger.info(f"[WEBHOOK] 收到: {json.dumps(data)[:600]}")

        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "ok"}), 200

        for msg in messages:
            from_number = msg.get("from", "")
            msg_type = msg.get("type", "")

            if msg_type == "text":
                content = msg.get("text", {})
            elif msg_type == "audio":
                content = msg.get("audio", {})
            else:
                content = msg.get(msg_type, {})

            process_message(from_number, msg_type, content)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"[WEBHOOK ERROR] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/tts", methods=["POST"])
def test_tts():
    """測試 MiniMax TTS"""
    data = request.get_json(force=True)
    text = data.get("text", "你好，我係 KIDS FIT 嘅 Arts。")
    try:
        audio_bytes = text_to_speech(text)
        return jsonify({"status": "ok", "audio_size_bytes": len(audio_bytes)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/send-text", methods=["POST"])
def test_send_text():
    """測試發送文字訊息"""
    data = request.get_json(force=True)
    to = data.get("to", "85268993194")
    message = data.get("message", "Maggie 系統測試")
    try:
        result = send_whatsapp_text(to, message)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/debug/state", methods=["GET"])
def debug_state():
    """查看當前所有用戶狀態（除錯用）"""
    states = _load_states()
    return jsonify({
        "user_states": states,
        "history_counts": {k: len(v) for k, v in _load_history().items()}
    })


# ─── Google Calendar 工具函數 ────────────────────────────────────────────────

def get_google_access_token() -> str:
    """
    從 Render 環境變數取得 Google OAuth2 Access Token。
    使用 GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET 換取 access token。
    如果直接有 GOOGLE_ACCESS_TOKEN 則直接使用。
    """
    # 優先使用直接 access token（短期）
    direct_token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
    if direct_token:
        return direct_token

    # 使用 refresh token 換取 access token
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    if not all([refresh_token, client_id, client_secret]):
        raise ValueError("缺少 Google OAuth2 憑證（GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET）")

    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_tomorrow_events() -> list:
    """從 Google Calendar 讀取明天的所有行程"""
    now_hk = datetime.now(HK_TZ)
    tomorrow_hk = now_hk + timedelta(days=1)
    time_min = tomorrow_hk.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    time_max = tomorrow_hk.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    token = get_google_access_token()
    url = f"{GCAL_API_BASE}/calendars/{GCAL_CALENDAR_ID}/events"
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 100,
    }
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def format_time_hk(dt_str: str) -> str:
    """將 RFC3339 時間字串格式化為香港時間顯示，例如 '上午8:30'"""
    if not dt_str:
        return "未知時間"
    try:
        dt = datetime.fromisoformat(dt_str)
        dt_hk = dt.astimezone(HK_TZ)
        hour = dt_hk.hour
        minute = dt_hk.minute
        period = "上午" if hour < 12 else "下午"
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        return f"{period}{display_hour}:{minute:02d}"
    except Exception:
        return dt_str[:16]


def extract_coach_from_description(description: str) -> str:
    """
    從行程備註中提取教練名稱。
    格式：「教練：XXX」
    返回教練名稱，如果為空或找不到則返回空字串。
    """
    if not description:
        return ""
    m = re.search(r'教練[：:]+\s*([^\n\r\s]+)', description)
    if m:
        name = m.group(1).strip()
        # 如果提取到的名稱是已知教練名，返回它
        if name in COACH_NUMBERS:
            return name
        # 如果提取到但不在列表中，也返回（讓上層處理）
        return name
    return ""


def send_daily_reminders():
    """
    每日下午6:00 執行：
    1. 讀取明天行程
    2. 根據教練名稱發送工作提示
    3. 發完整行程給大王留底
    """
    logger.info("[SCHEDULER] 開始發送每日工作提示...")
    now_hk = datetime.now(HK_TZ)
    tomorrow_hk = now_hk + timedelta(days=1)
    tomorrow_str = tomorrow_hk.strftime("%m月%d日")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    tomorrow_weekday = weekday_names[tomorrow_hk.weekday()]

    try:
        events = fetch_tomorrow_events()
    except Exception as e:
        logger.error(f"[SCHEDULER] 讀取日曆失敗: {e}")
        send_whatsapp_text(DAWANG_NUMBER, f"每日提示失敗：無法讀取 Google Calendar。\n錯誤：{str(e)[:100]}")
        return

    if not events:
        send_whatsapp_text(DAWANG_NUMBER, f"大王，明天（{tomorrow_str} {tomorrow_weekday}）Google Calendar 沒有行程。")
        logger.info("[SCHEDULER] 明天沒有行程")
        return

    # 按教練分組
    coach_events = {}   # {coach_name: [event, ...]}
    no_coach_events = []  # 沒有填教練的行程

    for event in events:
        summary = event.get("summary", "").strip()
        description = event.get("description", "") or ""
        start_dt = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        end_dt = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", ""))

        coach = extract_coach_from_description(description)

        event_info = {
            "summary": summary,
            "start": start_dt,
            "end": end_dt,
            "coach": coach,
        }

        if coach and coach in COACH_NUMBERS:
            if coach not in coach_events:
                coach_events[coach] = []
            coach_events[coach].append(event_info)
        else:
            no_coach_events.append(event_info)

    # 發送給各教練
    notified_coaches = []

    for coach_name, evts in coach_events.items():
        wa_number = COACH_NUMBERS[coach_name]
        if len(evts) == 1:
            e = evts[0]
            msg = (
                f"明天工作安排（{tomorrow_str} {tomorrow_weekday}）：\n\n"
                f"{e['summary']}\n"
                f"時間：{format_time_hk(e['start'])} - {format_time_hk(e['end'])}\n\n"
                f"如有任何問題，請聯絡大王。"
            )
        else:
            lines = [f"明天工作安排（{tomorrow_str} {tomorrow_weekday}）：\n"]
            for i, e in enumerate(evts, 1):
                lines.append(
                    f"{i}. {e['summary']}\n"
                    f"   時間：{format_time_hk(e['start'])} - {format_time_hk(e['end'])}"
                )
            lines.append("\n如有任何問題，請聯絡大王。")
            msg = "\n".join(lines)

        result = send_whatsapp_text(wa_number, msg)
        if "messages" in result:
            notified_coaches.append(coach_name)
            logger.info(f"[SCHEDULER] 已發送給 {coach_name}（{wa_number}）")
        else:
            err = result.get("error", {}).get("message", "未知錯誤")
            logger.error(f"[SCHEDULER] 發送給 {coach_name} 失敗: {err}")

    # 如有未填教練的行程，通知所有教練
    if no_coach_events:
        alert_msg = f"明天（{tomorrow_str} {tomorrow_weekday}）的人手分配尚未填妥，請留意稍後通知。"
        for coach_name, wa_number in COACH_NUMBERS.items():
            send_whatsapp_text(wa_number, alert_msg)
            logger.info(f"[SCHEDULER] 已發送人手未填通知給 {coach_name}")

    # 發完整行程給大王留底
    summary_lines = [f"大王，以下是明天（{tomorrow_str} {tomorrow_weekday}）完整行程：\n"]
    for event in events:
        summary = event.get("summary", "").strip()
        description = event.get("description", "") or ""
        start_dt = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        end_dt = event.get("end", {}).get("dateTime", event.get("end", {}).get("date", ""))
        coach = extract_coach_from_description(description)
        coach_label = f"（教練：{coach}）" if coach else "（教練：未填）"
        summary_lines.append(
            f"- {summary} {coach_label}\n"
            f"  時間：{format_time_hk(start_dt)} - {format_time_hk(end_dt)}"
        )

    if notified_coaches:
        summary_lines.append(f"\n已發送工作提示給：{', '.join(notified_coaches)}")
    if no_coach_events:
        summary_lines.append(f"未填教練行程：{len(no_coach_events)} 個，已通知所有教練留意。")

    send_whatsapp_text(DAWANG_NUMBER, "\n".join(summary_lines))
    logger.info(f"[SCHEDULER] 完成。已通知 {len(notified_coaches)} 位教練")


@app.route("/admin/send-reminders", methods=["POST"])
def manual_send_reminders():
    """手動觸發每日提示（測試用）"""
    try:
        threading.Thread(target=send_daily_reminders, daemon=True).start()
        return jsonify({"status": "ok", "message": "已觸發發送每日工作提示"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── APScheduler 定時任務 ────────────────────────────────────────────────────

def start_scheduler():
    """啟動 APScheduler，每日下午6:00（香港時間）發送工作提示"""
    scheduler = BackgroundScheduler(timezone=HK_TZ)
    scheduler.add_job(
        send_daily_reminders,
        trigger="cron",
        hour=18,
        minute=0,
        id="daily_reminders",
        name="每日教練工作提示",
        replace_existing=True,
        misfire_grace_time=300,  # 允許5分鐘誤差
    )
    scheduler.start()
    logger.info("[SCHEDULER] APScheduler 已啟動，每日 18:00 HKT 發送工作提示")
    return scheduler


# 啟動 scheduler（在 gunicorn 中也會執行）
_scheduler = start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
