# WeChat Assistant Plugin Rules

These rules apply **only** when the current user turn originates from WeChat (messages beginning with `[微信小助理消息 · ...]`), or when the user explicitly asks to interact with WeChat / WeFlow. Do **not** alter normal desktop Markdown formatting or `file:///` links during regular desktop sessions.

## 1. Mobile WeChat Formatting (for `[微信小助理消息 ...]` turns)
- Your final text response is automatically pushed back to the user's phone via the plugin `Stop` hook.
- Keep responses concise, scannable, and mobile-friendly: short paragraphs, clear bullet points, minimal wide tables, and plain filenames/paths instead of `file:///` links (which are not clickable inside WeChat).

## 2. Native Multimodal Voice & Attachments
- Inbound WeChat voice messages are automatically downloaded and decoded from SILK v3 into playable `.wav` audio files (`- [audio] ... -> <path>`).
- **Always call `read_file` on `[audio]` attachments first** to listen directly to the user's real voice, tone, and intent, then respond naturally as a normal conversation (do not mechanically summarize the audio as a forwarded file).
- For forwarded images, PDFs, documents, or videos, proactively call `read_file` on the saved inbox path and handle them according to the user's instruction.

## 3. Sending Files, Images, or Text Back to WeChat
- When the user asks to send a file, report, PDF, or image back to WeChat, call the plugin's built-in MCP tools `wechat_send_file`, `wechat_send_image`, or `wechat_send_text` directly.
- If MCP tools are temporarily unavailable, fall back to running `send_reply.py --file <path>` or `--image <path>` as indicated in the inbound message footer.

## 4. Querying Local WeChat Chats & Favorites (WeFlow)
- When the user asks to search WeChat chat history, summarize a group chat, or read saved WeChat Official Account favorites/articles, use the integrated WeFlow MCP tools (`wechat_list_sessions`, `wechat_get_messages`, `wechat_search_chats`, `wechat_search_favorites`, `wechat_read_favorite`, `wechat_fetch_article`, etc.).
- If WeFlow is not yet installed or initialized, direct the user to run `/wechat-setup` (or call `wechat_assistant_status` to inspect what is missing).
