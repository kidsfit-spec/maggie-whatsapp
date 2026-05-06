# Maggie WhatsApp 溝通教練

KIDS FIT 私人溝通教練 WhatsApp Bot，幫助老闆將直接表達改寫為友善自然的廣東話版本，並以語音訊息發送給指定對象。

## 功能

- 接收文字/語音訊息（來自白名單用戶）
- OpenAI Whisper STT 語音轉文字
- Gemini AI 改寫訊息為友善廣東話版本
- 用戶確認後，MiniMax TTS 生成廣東話語音
- 發送語音訊息到指定 WhatsApp 號碼

## 使用流程

1. 用戶發訊息給 Maggie（格式：`發給 [電話號碼]: [想說的內容]`）
2. Maggie 改寫為友善版本，以文字回覆用戶確認
3. 用戶回覆「OK」、「Send」、「得」等確認
4. Maggie 生成廣東話語音，發送到指定號碼

## 環境變數

| 變數 | 說明 |
|------|------|
| `OPENAI_API_KEY` | OpenAI API Key（Whisper STT）|
| `GEMINI_API_KEY` | Google Gemini API Key |
| `PORT` | 服務端口（Render 自動設定）|

## Webhook 設定

- Webhook URL: `https://[your-render-domain]/webhook/whatsapp`
- Verify Token: `kidsfit_maggie_2024`

## 部署

本服務部署於 Render，連接 GitHub repo 自動部署。
