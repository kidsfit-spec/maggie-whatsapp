"""
Maggie / AIRTS WhatsApp 溝通系統
KIDS FIT

對內身份：Maggie（溝通教練，與用戶 Arts Mak 溝通）
對外身份：AIRTS（AI 代言人，代表 Arts Mak 向第三方發言）

對話狀態機（簡化版）：
  IDLE → 用戶發訊息 → Maggie 改寫 → AWAITING_CONFIRM
  AWAITING_CONFIRM → 用戶確認 → 生成語音 → 發回給用戶 → IDLE

用戶收到語音後可自行轉發到任何對象或群組。
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

WHITELIST = {"85268993194"}

# ─── 狀態管理（in-memory）────────────────────────────────────────────────────

STATE_IDLE = "idle"
STATE_AWAITING_CONFIRM = "awaiting_confirm"

# user_state[from_number] = {
#   "state": STATE_IDLE | STATE_AWAITING_CONFIRM,
#   "rewritten_text": str,  # 改寫後的語音稿（待 TTS）
# }
user_state: dict[str, dict] = {}

# 對話歷史，每個用戶保留最近8條
conversation_history: dict[str, list] = {}

# ─── AI 系統提示 ──────────────────────────────────────────────────────────────

MAGGIE_SYSTEM_PROMPT = """你係 Arts Mak 本人，KIDS FIT 嘅老闆，男性，說話率直但友善。

你嘅任務：
將 Arts 想表達嘅內容，改寫為友善自然嘅廣東話語音稿，準備發送給香港幼稚園嘅女性教育工作者（校長、主任、老師）。

最重要嘅原則（必須嚴格遵守）：
- 語音稿必須用第一人稱「我」說話，你就係 Arts Mak 本人
- 絕對唔可以出現「Arts」「Arts Mak」「代表 Arts」呢類第三人稱表述
- 唔可以用代言人語氣，例如「我係代表 Arts 嘅 AIRTS」
- 直接以「我」嘅身份開口說話

改寫原則：
1. 保留原意，不改變核心訊息
2. 語氣友善但不諂媚，真誠不虛偽，像朋友之間講嘢咁自然
3. 符合香港商業溝通習慣，用詞得體
4. 廣東話口語風格，自然流暢，適合朗讀
5. 長度適中，不要過長
6. 不使用 Emoji

正確示範：
- 「我記得上次都有提過...」（正確）
- 「我哋 KIDS FIT 最近有個新課程...」（正確）
- 「我想約個時間同你傾下...」（正確）

錯誤示範（絕對禁止）：
- 「Arts 記得上次都有提過...」（錯誤，第三人稱）
- 「我係代表 Arts 嘅 AIRTS...」（錯誤，代言人語氣）
- 「Arts Mak 想話...」（錯誤，第三人稱）

回覆格式要求（非常重要）：
- 你的回覆必須分為兩部分，用 "---" 分隔
- 第一部分：純粹的語音稿內容（將會被 TTS 朗讀的文字，不要加任何標記或引號）
- 第二部分：給 Arts 的確認訊息
- 範例格式：

校長你好，我係 Arts，KIDS FIT 嘅負責人。我哋最近有個新嘅體能課程方案，想睇下貴校有冇興趣了解一下。方便嘅話我哋可以約個時間傾下。
---
以上係改寫後嘅語音稿。確認OK請回覆「OK」或「好」，我會即刻生成語音俾你轉發。如要取消請回覆「取消」。

重要：
- 語音稿部分要適合廣東話朗讀，自然流暢
- 確認訊息用繁體中文
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
    logger.info(f"[WA AUDIO] to={to} status={resp.status_code}")
    return resp.json()


# ─── STT（Gemini 多模態語音識別）──────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """使用 Gemini 多模態 API 將語音轉為文字（取代 OpenAI Whisper，避免速率限制）"""
    import base64

    # 根據副檔名決定 MIME type
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
        "generationConfig": {
            "temperature": 0
        }
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
            "speed": 0.9,      # 略慢語速，廣東話更清晰自然
            "vol": 1.0,        # 標準音量
            "pitch": 0         # 原音色音調
        },
        "audio_setting": {
            "format": "mp3",
            "sample_rate": 32000,   # 高採樣率，音質更清晰
            "bitrate": 128000       # 128kbps，WhatsApp 語音訊息標準
        }
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
    """取得用戶當前狀態"""
    if user_id not in user_state:
        user_state[user_id] = {
            "state": STATE_IDLE,
            "rewritten_text": "",
        }
    return user_state[user_id]


def reset_user_state(user_id: str):
    """重置用戶狀態為 IDLE"""
    user_state[user_id] = {
        "state": STATE_IDLE,
        "rewritten_text": "",
    }


def is_confirmation(text: str) -> bool:
    """判斷用戶是否確認發送"""
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
    """判斷用戶是否取消"""
    text_lower = text.lower().strip()
    cancels = {"唔好", "取消", "cancel", "算", "唔使", "唔洗", "no", "唔要", "重嚟", "重來"}
    if text_lower in cancels:
        return True
    cancel_phrases = ["唔好意思", "算啦", "唔使喇", "取消發送"]
    return any(p in text_lower for p in cancel_phrases)


def parse_gemini_response(response: str) -> tuple:
    """
    解析 Gemini 回覆，分離語音稿和確認訊息
    返回 (voice_script, full_reply_to_user)
    """
    # 嘗試用 --- 分隔符分割
    if "---" in response:
        parts = response.split("---", 1)
        voice_script = parts[0].strip()
        confirm_msg = parts[1].strip() if len(parts) > 1 else ""
        if voice_script and len(voice_script) > 10:
            return voice_script, response

    # 備用：嘗試提取引號內容
    quote_patterns = [
        r'「([\s\S]+?)」',
        r'"([\s\S]+?)"',
    ]
    for pattern in quote_patterns:
        matches = re.findall(pattern, response)
        if matches:
            longest = max(matches, key=len)
            if len(longest) > 15:
                return longest.strip(), response

    # 最後手段：取第一段作為語音稿
    paragraphs = [p.strip() for p in response.split('\n\n') if p.strip()]
    for para in paragraphs:
        if len(para) > 15 and not any(kw in para for kw in ["確認", "請問", "是否", "OK", "Send", "回覆"]):
            return para, response

    return response.strip(), response


# ─── 主要訊息處理邏輯（狀態機）────────────────────────────────────────────────

def process_message(from_number: str, msg_type: str, msg_content: dict):
    """處理接收到的 WhatsApp 訊息 - 基於狀態機"""

    # 白名單檢查
    if from_number not in WHITELIST:
        logger.warning(f"[WHITELIST] 拒絕: {from_number}")
        send_whatsapp_text(from_number, "此服務僅限授權用戶使用。")
        return

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
        send_whatsapp_text(from_number, "AIRTS 目前支援文字和語音訊息。")
        return

    if not text:
        return

    # 取得當前狀態
    state = get_user_state(from_number)
    current_state = state["state"]
    logger.info(f"[STATE] user={from_number} state={current_state} input={text[:50]}")

    try:
        # ═══════════════════════════════════════════════════════════════════
        # 狀態：等待用戶確認改寫內容
        # ═══════════════════════════════════════════════════════════════════
        if current_state == STATE_AWAITING_CONFIRM:
            # 檢查是否取消
            if is_cancel(text):
                reset_user_state(from_number)
                send_whatsapp_text(from_number, "已取消。有新訊息隨時再發給我。")
                return

            # 檢查是否確認
            if is_confirmation(text):
                # 用戶確認了，生成語音並發回給用戶
                _execute_generate_and_send_back(from_number)
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
    """處理新的改寫請求"""

    if not text.strip():
        send_whatsapp_text(
            from_number,
            "請告訴我您想說什麼，我幫你改寫為友善自然的版本。"
        )
        return

    # 呼叫 Gemini 改寫
    prompt = f"我想表達：「{text}」\n\n請用第一人稱「我」改寫為友善自然的廣東話語音稿。記住：你就係我本人，絕對唔可以出現 Arts 呢類第三人稱。"
    gemini_reply = call_gemini(prompt, from_number)

    # 解析回覆
    voice_script, full_reply = parse_gemini_response(gemini_reply)

    # 更新對話歷史
    add_to_history(from_number, "user", text)
    add_to_history(from_number, "assistant", gemini_reply)

    # 設定狀態為等待確認
    state = get_user_state(from_number)
    state["state"] = STATE_AWAITING_CONFIRM
    state["rewritten_text"] = voice_script

    # 回覆用戶（Gemini 的完整回覆已包含確認提示）
    send_whatsapp_text(from_number, full_reply)


def _execute_generate_and_send_back(from_number: str):
    """生成語音並發回給用戶"""
    state = get_user_state(from_number)
    rewritten_text = state.get("rewritten_text", "")

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

        # 發送語音回給用戶
        result = send_whatsapp_audio(from_number, media_id)
        logger.info(f"[SEND BACK] 結果: {result}")

        # 通知用戶
        send_whatsapp_text(
            from_number,
            "語音已生成！你可以長按上面嘅語音訊息轉發俾任何人或群組。"
        )

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
        "service": "AIRTS WhatsApp 溝通系統",
        "description": "KIDS FIT AI 溝通助手 AIRTS",
        "status": "running",
        "version": "2.3.6",
        "flow": "用戶發訊息 → AIRTS改寫 → 用戶確認 → 生成語音發回用戶 → 用戶自行轉發"
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
    text = data.get("text", "你好，我係 AIRTS，代表 Arts Mak 向你問好。")
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
    message = data.get("message", "AIRTS 系統測試 - v2.3")
    try:
        result = send_whatsapp_text(to, message)
        return jsonify({"status": "ok", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/debug/state", methods=["GET"])
def debug_state():
    """查看當前所有用戶狀態（除錯用）"""
    return jsonify({
        "user_states": {k: v["state"] for k, v in user_state.items()},
        "history_counts": {k: len(v) for k, v in conversation_history.items()}
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
