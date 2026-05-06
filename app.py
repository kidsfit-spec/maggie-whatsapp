"""
Maggie / AIRTS WhatsApp 溝通系統
KIDS FIT

對內身份：Maggie（溝通教練，與用戶 Arts Mak 溝通）
對外身份：AIRTS（AI 代言人，代表 Arts Mak 向第三方發言）
"""

import os
import json
import logging
import tempfile
import requests
import re
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

# WhatsApp Business API（Maggie 的號碼：+852 9578 9829）
WA_PHONE_NUMBER_ID = "780503051810575"
WA_ACCESS_TOKEN = (
    "EAAdmmySD1n8BReIPTLEoC6419ZATVKuT7IEopr8yJGB52ywYGisjHEa4ghiNESbXmAlDPZBx"
    "DiarYzpLrjCSA00Rb9KpF2drAiaNr0IzKWmceqv590OUazCqjL9KQgi7yAkn2N0dRvDXjZAew"
    "ObodPH2ppegvKl9AiN702f5D0SfixeJHsnylBOisT0VwZDZD"
)
WA_VERIFY_TOKEN = "kidsfit_maggie_2024"
WA_API_BASE = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}"

# MiniMax TTS（廣東話語音生成）
MINIMAX_API_KEY = (
    "sk-api-3hXH9X2CMVPLfIIsWCGVoqcG4XQXF83T1jus6_UPu4DDW-jy4-1ctvb1a44X_-"
    "mURow51iEiV3VbNGQxMh7Pw3qjrUbIAdNTTyKJITjFhlCbaw2GxwSAQy0"
)
MINIMAX_VOICE_ID = "moss_audio_044256f0-48e5-11f1-9ac6-fa4383f073f0"
MINIMAX_MODEL = "speech-2.8-hd"
MINIMAX_ENDPOINT = "https://api.minimax.io/v1/t2a_v2"

# OpenAI（Whisper STT）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Gemini AI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 白名單：只有 Arts Mak 的號碼可使用
WHITELIST = {"85268993194"}

# ─── 狀態管理（in-memory）────────────────────────────────────────────────────

# 對話歷史，每個用戶保留最近8條
conversation_history: dict[str, list] = {}

# 等待確認狀態
# pending_send[from_number] = {
#   "rewritten_text": str,   # 要發送的改寫內容（廣東話，以 AIRTS 身份）
#   "target_number": str,    # 目標電話號碼
#   "preview_text": str      # 給用戶看的確認文字
# }
pending_send: dict[str, dict] = {}

# ─── AI 系統提示 ──────────────────────────────────────────────────────────────

MAGGIE_SYSTEM_PROMPT = """你是 Maggie，Arts Mak 的私人溝通教練助手。Arts Mak 是 KIDS FIT 的老闆，男性，說話率直。

你的任務：
幫助 Arts 將想表達的內容改寫為友善自然的版本，準備以廣東話語音（以 AIRTS 的身份）發送給香港幼稚園的女性教育工作者（校長、主任、老師）。

改寫原則：
1. 保留原意，不改變核心訊息
2. 語氣友善但不諂媚，真誠不虛偽
3. 符合香港商業溝通習慣，用詞得體
4. 廣東話口語風格，自然流暢，適合朗讀
5. 長度適中，不要過長
6. 不使用 Emoji

關於 AIRTS 身份：
- 對外發送的語音訊息，發送者身份是 AIRTS（AI Representative of Arts Mak）
- AIRTS 代表 Arts Mak 向對方發言
- 如果改寫內容需要自我介紹，應用「我係 AIRTS，代表 Arts Mak」或類似表達
- 但通常不需要特別介紹，直接說訊息內容即可

工作流程：
1. 分析 Arts 想表達的內容
2. 改寫為友善自然的廣東話版本
3. 以文字回覆 Arts 確認（用繁體中文）

回覆格式（對 Arts）：
- 先展示改寫後的語音稿
- 然後問是否確認發送
- 語氣親切但專業

重要：
- 與 Arts 溝通時用繁體中文
- 改寫的語音稿要適合廣東話朗讀
- 不使用 Emoji
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

    # 取得媒體 URL
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    media_url = resp.json().get("url")

    # 下載媒體內容
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
    media_id = resp.json().get("id")
    logger.info(f"[WA UPLOAD] media_id={media_id}")
    return media_id


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
    logger.info(f"[WA AUDIO] to={to} status={resp.status_code}")
    return resp.json()


# ─── STT（OpenAI Whisper）────────────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """使用 OpenAI Whisper 將音頻轉為文字"""
    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="zh"
            )
        logger.info(f"[STT] 識別結果: {transcript.text[:100]}")
        return transcript.text
    finally:
        os.unlink(tmp_path)


# ─── Gemini AI 改寫 ───────────────────────────────────────────────────────────

def get_history(user_id: str) -> list:
    return conversation_history.get(user_id, [])


def add_to_history(user_id: str, role: str, content: str):
    """新增對話記錄，保留最近8條"""
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
            "voice_id": MINIMAX_VOICE_ID
        },
        "audio_setting": {
            "format": "mp3"
        }
    }
    resp = requests.post(MINIMAX_ENDPOINT, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    audio_hex = data.get("data", {}).get("audio", "")
    if not audio_hex:
        raise ValueError(f"MiniMax TTS 返回空音頻，回應：{json.dumps(data)[:300]}")

    audio_bytes = bytes.fromhex(audio_hex)
    logger.info(f"[TTS] 生成音頻 {len(audio_bytes)} bytes")
    return audio_bytes


# ─── 訊息解析工具 ─────────────────────────────────────────────────────────────

def extract_target_number(text: str) -> tuple:
    """
    從訊息中提取目標電話號碼與訊息內容
    返回 (target_number | None, cleaned_message)

    支援格式：
    - 發給 852XXXXXXXX: 訊息內容
    - 發到 +852XXXXXXXX 訊息內容
    - 號碼 852XXXXXXXX: 訊息內容
    - 852XXXXXXXX: 訊息內容（號碼在開頭）
    """
    patterns = [
        # 明確關鍵字 + 號碼 + 內容
        r'(?:發給|發到|傳給|傳到|send\s*to|to)[：:\s]+\+?(\d{8,15})[：:\s]*([\s\S]+)',
        r'(?:號碼|number)[：:\s]+\+?(\d{8,15})[：:\s]*([\s\S]+)',
        # 號碼在開頭
        r'^\+?(\d{8,15})[：:\s]+([\s\S]+)',
        # 號碼在末尾（內容 + 號碼）
        r'([\s\S]+?)[，,\s]+(?:發給|發到|傳給|傳到)\+?(\d{8,15})\s*$',
    ]

    for i, pattern in enumerate(patterns):
        match = re.search(pattern, text.strip(), re.IGNORECASE)
        if match:
            if i < 3:
                number = match.group(1).strip().lstrip("+")
                message = match.group(2).strip()
            else:
                # 號碼在末尾的格式
                message = match.group(1).strip()
                number = match.group(2).strip().lstrip("+")

            # 補全香港國碼
            if len(number) == 8 and number[0] in "2345679":
                number = "852" + number

            if len(number) >= 10 and message:
                logger.info(f"[PARSE] 提取號碼={number} 訊息={message[:50]}")
                return number, message

    return None, text.strip()


def is_confirmation(text: str) -> bool:
    """判斷用戶是否確認發送"""
    text_lower = text.lower().strip()
    # 精確匹配確認詞（避免誤判）
    exact_confirmations = {
        "ok", "send", "得", "發", "確認", "yes", "係",
        "好", "發送", "ok la", "得la", "得喇", "ok喇",
        "send喇", "傳送", "go", "go ahead", "傳"
    }
    # 完整匹配
    if text_lower in exact_confirmations:
        return True
    # 包含確認詞（較長的詞組）
    long_confirmations = ["ok喇", "send喇", "得喇", "ok la", "go ahead", "確認發送"]
    return any(conf in text_lower for conf in long_confirmations)


def extract_voice_script(gemini_response: str) -> str:
    """
    從 Gemini 回覆中提取要發送的語音稿
    Gemini 會回覆確認訊息，我們需要提取實際的語音稿內容
    """
    # 嘗試提取標記區塊
    block_patterns = [
        r'語音稿[：:]\s*\n?([\s\S]+?)(?:\n\n|請問|是否|確認|$)',
        r'改寫[版本]*[：:]\s*\n?([\s\S]+?)(?:\n\n|請問|是否|確認|$)',
        r'建議版本[：:]\s*\n?([\s\S]+?)(?:\n\n|請問|是否|確認|$)',
        r'以下[係是]改寫[後]?[版本]*[：:]\s*\n?([\s\S]+?)(?:\n\n|請問|是否|確認|$)',
    ]
    for pattern in block_patterns:
        match = re.search(pattern, gemini_response, re.DOTALL)
        if match:
            return match.group(1).strip()

    # 嘗試提取引號內容（書名號或引號）
    quote_patterns = [
        r'「([\s\S]+?)」',
        r'"([\s\S]+?)"',
        r'【([\s\S]+?)】',
    ]
    for pattern in quote_patterns:
        matches = re.findall(pattern, gemini_response)
        if matches:
            # 取最長的引號內容（通常是語音稿）
            longest = max(matches, key=len)
            if len(longest) > 15:
                return longest.strip()

    # 取第一個實質段落（跳過短句）
    paragraphs = [p.strip() for p in gemini_response.split('\n\n') if p.strip()]
    for para in paragraphs:
        if len(para) > 20 and not any(kw in para for kw in ["確認", "請問", "是否", "OK", "Send"]):
            return para.strip()

    # 最後手段：返回整個回覆
    return gemini_response.strip()


# ─── 主要訊息處理邏輯 ─────────────────────────────────────────────────────────

def process_message(from_number: str, msg_type: str, msg_content: dict):
    """處理接收到的 WhatsApp 訊息"""

    # 白名單檢查
    if from_number not in WHITELIST:
        logger.warning(f"[WHITELIST] 拒絕非授權號碼: {from_number}")
        send_whatsapp_text(
            from_number,
            "此服務僅限授權用戶使用。"
        )
        return

    logger.info(f"[MSG] from={from_number} type={msg_type}")

    try:
        # ── 文字訊息 ──────────────────────────────────────────────────────
        if msg_type == "text":
            text = msg_content.get("body", "").strip()
            if not text:
                return

            # 檢查是否有待確認的發送任務
            if from_number in pending_send and is_confirmation(text):
                _execute_pending_send(from_number)
                return

            # 如果有待確認但收到新訊息（非確認），清除舊任務
            if from_number in pending_send:
                del pending_send[from_number]
                send_whatsapp_text(from_number, "已取消上一個發送任務，處理新請求中...")

            # 解析目標號碼與訊息內容
            target_number, clean_message = extract_target_number(text)
            _handle_rewrite_request(from_number, clean_message, target_number, original_text=text)

        # ── 語音訊息 ──────────────────────────────────────────────────────
        elif msg_type == "audio":
            audio_id = msg_content.get("id")
            if not audio_id:
                send_whatsapp_text(from_number, "無法讀取語音訊息，請重試。")
                return

            send_whatsapp_text(from_number, "收到語音訊息，正在識別中...")

            # 下載並轉錄語音
            audio_bytes = download_whatsapp_media(audio_id)
            transcribed = transcribe_audio(audio_bytes, suffix=".ogg")

            if not transcribed.strip():
                send_whatsapp_text(from_number, "無法識別語音內容，請重試或改用文字訊息。")
                return

            # 如果有待確認任務，先清除
            if from_number in pending_send:
                del pending_send[from_number]

            # 解析目標號碼
            target_number, clean_message = extract_target_number(transcribed)
            _handle_rewrite_request(
                from_number,
                clean_message,
                target_number,
                original_text=transcribed,
                is_voice=True
            )

        else:
            send_whatsapp_text(from_number, "Maggie 目前支援文字和語音訊息。")

    except Exception as e:
        logger.error(f"[ERROR] 處理訊息失敗: {e}", exc_info=True)
        send_whatsapp_text(from_number, "系統發生錯誤，請稍後再試。")


def _handle_rewrite_request(
    from_number: str,
    message: str,
    target_number: str | None,
    original_text: str = "",
    is_voice: bool = False
):
    """處理改寫請求，呼叫 Gemini 並回覆用戶"""

    if not message:
        send_whatsapp_text(
            from_number,
            "請告訴我您想說什麼，以及發給誰的電話號碼。\n\n"
            "例如：發給 85291234567: 想了解貴校對體能課程的興趣\n"
            "或：85291234567: 下星期想約時間拜訪"
        )
        return

    # 構建給 Gemini 的提示
    if is_voice:
        prompt = (
            f"Arts 用語音說了以下內容：\n「{original_text}」\n\n"
            f"想表達的核心訊息：{message}\n\n"
            f"請改寫為友善自然的廣東話語音稿，以 AIRTS 身份代表 Arts Mak 發言。"
        )
    else:
        prompt = (
            f"Arts 想表達：\n「{message}」\n\n"
            f"請改寫為友善自然的廣東話語音稿，以 AIRTS 身份代表 Arts Mak 發言。"
        )

    if target_number:
        prompt += f"\n\n（語音將發送到：+{target_number}）"

    # 呼叫 Gemini
    gemini_reply = call_gemini(prompt, from_number)

    # 更新對話歷史
    add_to_history(from_number, "user", original_text or message)
    add_to_history(from_number, "assistant", gemini_reply)

    # 提取語音稿
    voice_script = extract_voice_script(gemini_reply)

    # 儲存待確認狀態
    if target_number:
        pending_send[from_number] = {
            "rewritten_text": voice_script,
            "target_number": target_number,
            "preview_text": gemini_reply
        }
        # 回覆用戶確認
        send_whatsapp_text(from_number, gemini_reply)
    else:
        # 沒有目標號碼，先給改寫結果，再詢問號碼
        pending_send[from_number] = {
            "rewritten_text": voice_script,
            "target_number": None,
            "preview_text": gemini_reply
        }
        send_whatsapp_text(
            from_number,
            gemini_reply + "\n\n請問要發送到哪個電話號碼？（直接回覆號碼即可）"
        )


def _execute_pending_send(from_number: str):
    """執行待確認的語音發送任務"""
    pending = pending_send.get(from_number)
    if not pending:
        return

    target_number = pending.get("target_number")
    rewritten_text = pending.get("rewritten_text", "")

    # 如果還沒有目標號碼，等待用戶提供
    if not target_number:
        send_whatsapp_text(from_number, "請提供目標電話號碼。")
        return

    send_whatsapp_text(from_number, f"好的，正在生成語音並發送到 +{target_number}...")

    try:
        # 生成廣東話語音
        audio_bytes = text_to_speech(rewritten_text)

        # 上傳到 WhatsApp
        media_id = upload_whatsapp_audio(audio_bytes)

        # 發送語音到目標號碼
        result = send_whatsapp_audio(target_number, media_id)
        logger.info(f"[SEND] 語音發送結果: {result}")

        # 通知 Arts 發送成功
        send_whatsapp_text(
            from_number,
            f"語音訊息已成功發送到 +{target_number}。\n\n"
            f"發送內容：\n{rewritten_text}"
        )

        # 更新對話歷史
        add_to_history(from_number, "user", "[確認發送]")
        add_to_history(from_number, "assistant", f"語音已發送到 +{target_number}")

    except Exception as e:
        logger.error(f"[SEND ERROR] 發送語音失敗: {e}", exc_info=True)
        send_whatsapp_text(
            from_number,
            f"發送失敗，請稍後再試。\n錯誤：{str(e)[:150]}"
        )
    finally:
        # 清除待確認狀態
        if from_number in pending_send:
            del pending_send[from_number]


def _handle_number_reply(from_number: str, text: str):
    """處理用戶回覆目標號碼的情況"""
    # 提取號碼
    number_match = re.search(r'\+?(\d{8,15})', text.strip())
    if not number_match:
        return False

    number = number_match.group(1).lstrip("+")
    if len(number) == 8 and number[0] in "2345679":
        number = "852" + number

    if from_number in pending_send:
        pending_send[from_number]["target_number"] = number
        send_whatsapp_text(
            from_number,
            f"好的，將發送到 +{number}。\n\n"
            f"語音稿：\n{pending_send[from_number]['rewritten_text']}\n\n"
            f"確認發送請回覆「OK」或「Send」。"
        )
        return True

    return False


# ─── Flask 路由 ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Maggie / AIRTS WhatsApp 溝通系統",
        "description": "KIDS FIT AI 溝通教練 - 對內 Maggie，對外 AIRTS",
        "status": "running",
        "version": "2.0.0",
        "endpoints": {
            "webhook": "/webhook/whatsapp",
            "health": "/health",
            "test_tts": "POST /test/tts",
            "test_send": "POST /test/send-text"
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

    logger.info(f"[WEBHOOK] 驗證請求: mode={mode} token={token}")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        logger.info("[WEBHOOK] 驗證成功")
        return challenge, 200
    else:
        logger.warning(f"[WEBHOOK] 驗證失敗: token={token}")
        return "Forbidden", 403


@app.route("/webhook/whatsapp", methods=["POST"])
def webhook_receive():
    """接收 WhatsApp 訊息 Webhook"""
    try:
        data = request.get_json(force=True)
        logger.info(f"[WEBHOOK] 收到: {json.dumps(data)[:600]}")

        # 解析 WhatsApp Cloud API 格式
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            # 狀態更新或其他事件，忽略
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
    text = data.get("text", "你好，我係 AIRTS，代表 Arts Mak 向你問好。")

    try:
        audio_bytes = text_to_speech(text)
        return jsonify({
            "status": "ok",
            "audio_size_bytes": len(audio_bytes),
            "text": text
        })
    except Exception as e:
        logger.error(f"[TEST TTS] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/send-text", methods=["POST"])
def test_send_text():
    """測試發送文字訊息"""
    data = request.get_json(force=True)
    to = data.get("to", "85268993194")
    message = data.get("message", "Maggie 系統測試 - 一切正常運作中。")

    try:
        result = send_whatsapp_text(to, message)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        logger.error(f"[TEST SEND] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test/full-flow", methods=["POST"])
def test_full_flow():
    """測試完整流程（改寫 + TTS，不實際發送）"""
    data = request.get_json(force=True)
    message = data.get("message", "想了解貴校對體能課程的興趣")
    target = data.get("target", "85291234567")

    try:
        # 1. Gemini 改寫
        prompt = f"Arts 想表達：「{message}」\n請改寫為友善廣東話語音稿，以 AIRTS 身份代表 Arts Mak 發言。"
        rewritten = call_gemini(prompt, "test_user")
        voice_script = extract_voice_script(rewritten)

        # 2. TTS
        audio_bytes = text_to_speech(voice_script)

        return jsonify({
            "status": "ok",
            "original": message,
            "gemini_reply": rewritten,
            "voice_script": voice_script,
            "audio_size_bytes": len(audio_bytes),
            "target": target
        })
    except Exception as e:
        logger.error(f"[TEST FLOW] {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
