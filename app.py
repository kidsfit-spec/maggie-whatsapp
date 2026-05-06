"""
Maggie WhatsApp 溝通系統 v2.6.0
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
"""

import os
import json
import logging
import re
import requests
from flask import Flask, request, jsonify
from google import genai
from google.genai import types

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
MINIMAX_VOICE_ID = "presenter_female"
MINIMAX_MODEL = "speech-2.8-hd"
MINIMAX_ENDPOINT = "https://api.minimax.io/v1/t2a_v2"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 完整功能白名單
WHITELIST = {"85268993194", "85263951689"}

# 大王號碼（接收第三方轉發通知）
DAWANG_NUMBER = "85268993194"

# ─── 狀態管理（in-memory）────────────────────────────────────────────────────

STATE_IDLE = "idle"
STATE_AWAITING_CONFIRM = "awaiting_confirm"
STATE_AWAITING_NUMBER = "awaiting_number"

# user_state[from_number] = {
#   "state": STATE_IDLE | STATE_AWAITING_CONFIRM | STATE_AWAITING_NUMBER,
#   "rewritten_text": str,   # 改寫後的語音稿（待 TTS）
#   "target_number": str,    # 目標發送號碼（可能為空）
# }
user_state: dict[str, dict] = {}

# 對話歷史，每個用戶保留最近8條
conversation_history: dict[str, list] = {}

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

def get_history(user_id: str) -> list:
    return conversation_history.get(user_id, [])


def add_to_history(user_id: str, role: str, content: str):
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    conversation_history[user_id].append({"role": role, "content": content})
    if len(conversation_history[user_id]) > 8:
        conversation_history[user_id] = conversation_history[user_id][-8:]


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

def get_user_state(user_id: str) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {
            "state": STATE_IDLE,
            "rewritten_text": "",
            "target_number": "",
        }
    return user_state[user_id]


def reset_user_state(user_id: str):
    user_state[user_id] = {
        "state": STATE_IDLE,
        "rewritten_text": "",
        "target_number": "",
    }


def is_confirmation(text: str) -> bool:
    text_lower = text.lower().strip()
    exact_confirmations = {
        "ok", "send", "得", "發", "確認", "yes", "係",
        "好", "發送", "ok la", "得la", "得喇", "ok喇",
        "send喇", "傳送", "go", "go ahead", "傳",
        "可以", "okay", "收到", "好的"
    }
    if text_lower in exact_confirmations:
        return True
    long_confirmations = ["ok喇", "send喇", "得喇", "ok la", "go ahead", "確認發送", "可以發"]
    return any(conf in text_lower for conf in long_confirmations)


def is_cancel(text: str) -> bool:
    text_lower = text.lower().strip()
    cancels = {"唔好", "取消", "cancel", "算", "唔使", "唔洗", "no", "唔要", "重嚟", "重來"}
    if text_lower in cancels:
        return True
    cancel_phrases = ["唔好意思", "算啦", "唔使喇", "取消發送"]
    return any(p in text_lower for p in cancel_phrases)


def extract_phone_number(text: str) -> str:
    """
    從文字中提取香港電話號碼。
    支援格式：85212345678、+85212345678、12345678（8位本地號碼）
    返回標準格式（帶852前綴），找不到則返回空字串。
    """
    # 先嘗試匹配帶852前綴的11位號碼
    m = re.search(r'(?:\+?852)([235689]\d{7})', text)
    if m:
        return "852" + m.group(1)

    # 再嘗試匹配純8位香港號碼（2/3/5/6/9開頭），允許前面係中文字或空格
    m = re.search(r'(?:^|[\s\u4e00-\u9fff：:])([235689]\d{7})(?:[\s\u4e00-\u9fff：:,，。]|$)', text)
    if m:
        return "852" + m.group(1)

    return ""


def is_phone_number_only(text: str) -> str:
    """
    判斷訊息是否純粹是一個電話號碼（用於 AWAITING_NUMBER 狀態）。
    返回標準格式號碼或空字串。
    """
    clean = text.strip().replace(" ", "").replace("-", "").replace("+", "")
    # 11位帶852前綴
    if re.fullmatch(r'852[235689]\d{7}', clean):
        return clean
    # 8位本地號碼
    if re.fullmatch(r'[235689]\d{7}', clean):
        return "852" + clean
    return ""


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

    state = get_user_state(from_number)
    current_state = state["state"]
    logger.info(f"[STATE] user={from_number} state={current_state} input={text[:60]}")

    try:
        # ═══════════════════════════════════════════════════════════════════
        # 狀態：等待目標電話號碼
        # ═══════════════════════════════════════════════════════════════════
        if current_state == STATE_AWAITING_NUMBER:
            if is_cancel(text):
                reset_user_state(from_number)
                send_whatsapp_text(from_number, "已取消。有新訊息隨時再發給我。")
                return

            phone = is_phone_number_only(text)
            if phone:
                state["target_number"] = phone
                state["state"] = STATE_AWAITING_CONFIRM
                # 已有語音稿，直接執行發送
                _execute_generate_and_send(from_number)
            else:
                send_whatsapp_text(from_number, "大王，請輸入有效嘅香港電話號碼（例如：63951689 或 85263951689）。")
            return

        # ═══════════════════════════════════════════════════════════════════
        # 狀態：等待用戶確認改寫內容
        # ═══════════════════════════════════════════════════════════════════
        if current_state == STATE_AWAITING_CONFIRM:
            if is_cancel(text):
                reset_user_state(from_number)
                send_whatsapp_text(from_number, "已取消。有新訊息隨時再發給我。")
                return

            if is_confirmation(text):
                target = state.get("target_number", "")
                if not target and from_number == DAWANG_NUMBER:
                    # 大王確認但沒有目標號碼，問號碼
                    state["state"] = STATE_AWAITING_NUMBER
                    send_whatsapp_text(from_number, "大王，想發到邊個號碼？")
                    return
                # 有目標號碼或非大王（發回本人）
                _execute_generate_and_send(from_number)
                return

            # 不是確認也不是取消，當作新的改寫請求
            reset_user_state(from_number)
            _handle_new_message(from_number, text)
            return

        # ═══════════════════════════════════════════════════════════════════
        # 狀態：空閒（接收新訊息）
        # ═══════════════════════════════════════════════════════════════════
        else:  # STATE_IDLE
            _handle_new_message(from_number, text)

    except Exception as e:
        logger.error(f"[ERROR] 處理訊息失敗: {e}", exc_info=True)
        reset_user_state(from_number)
        send_whatsapp_text(from_number, "系統發生錯誤，請稍後再試。")


def _handle_new_message(from_number: str, text: str):
    """處理新的改寫請求，同時嘗試提取目標號碼"""

    if not text.strip():
        send_whatsapp_text(
            from_number,
            "大王，你想表達咩？話俾我知，我幫你改寫為友善自然嘅版本。"
        )
        return

    # 嘗試從訊息中提取目標號碼（大王專用）
    target_number = ""
    message_content = text
    if from_number == DAWANG_NUMBER:
        target_number = extract_phone_number(text)
        if target_number:
            # 移除號碼部分，保留訊息內容
            # 移除常見前綴如「同63951689講：」「幫我同85263951689講...」
            cleaned = re.sub(
                r'(?:幫我)?(?:同|發俾|發給|發到|告訴|通知)\s*(?:\+?852)?\s*\d{8,11}\s*(?:講|說|話|：|:)?\s*',
                '', text, flags=re.IGNORECASE
            ).strip()
            # 如果清理後有內容就用，否則用原文
            if cleaned and len(cleaned) > 3:
                message_content = cleaned
            logger.info(f"[EXTRACT] 目標號碼={target_number} 內容={message_content[:50]}")

    # 呼叫 Gemini 改寫
    prompt = f"大王想表達：「{message_content}」\n\n請用第一人稱「我」改寫為友善自然嘅廣東話語音稿。記住：語音稿係代表大王本人講嘅，唔可以出現第三人稱。"
    gemini_reply = call_gemini(prompt, from_number)

    # 解析回覆
    voice_script, full_reply = parse_gemini_response(gemini_reply)

    # 更新對話歷史
    add_to_history(from_number, "user", text)
    add_to_history(from_number, "assistant", gemini_reply)

    # 設定狀態
    state = get_user_state(from_number)
    state["state"] = STATE_AWAITING_CONFIRM
    state["rewritten_text"] = voice_script
    state["target_number"] = target_number

    # 回覆用戶
    send_whatsapp_text(from_number, full_reply)


def _execute_generate_and_send(from_number: str):
    """生成語音並發送"""
    state = get_user_state(from_number)
    rewritten_text = state.get("rewritten_text", "")
    target_number = state.get("target_number", "")

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
        if from_number == DAWANG_NUMBER and target_number:
            # 大王有指定目標號碼：直接發到對方 + 副本給大王
            target_display = format_phone_display(target_number)

            # 發到目標號碼
            target_result = send_whatsapp_audio(target_number, media_id)
            target_ok = "messages" in target_result
            logger.info(f"[SEND TO TARGET] to={target_number} ok={target_ok}")

            # 副本給大王
            send_whatsapp_audio(from_number, media_id)
            logger.info(f"[SEND COPY TO DAWANG] to={from_number}")

            if target_ok:
                send_whatsapp_text(from_number, f"大王，語音已直接發送到 {target_display}，副本已發回俾你留底。")
            else:
                err_msg = target_result.get("error", {}).get("message", "未知錯誤")
                send_whatsapp_text(from_number, f"大王，發送到 {target_display} 失敗：{err_msg[:100]}\n\n副本已發回俾你，可以手動轉發。")

        else:
            # 非大王 或 大王沒有指定目標號碼：發回本人
            send_whatsapp_audio(from_number, media_id)
            send_whatsapp_text(from_number, "大王，語音已生成，發回俾你留底。")

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
        "version": "2.6.1",
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
    return jsonify({
        "user_states": {k: {"state": v["state"], "target": v.get("target_number", "")} for k, v in user_state.items()},
        "history_counts": {k: len(v) for k, v in conversation_history.items()}
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
