import os
import re
import time
import sqlite3
from datetime import datetime, date
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from dotenv import load_dotenv

# LINE Messaging API v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Gemini
from google import genai
from google.genai.errors import ClientError

# ==========
# Settings
# ==========
JST = ZoneInfo("Asia/Tokyo")
DB_PATH = "bot_memory.sqlite"

# 使うモデル（あなたの一覧から。クォータ超過時に順に試す）
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-pro-latest",
]

DAILY_QUESTION = (
    "こんばんは！今日の就活・勉強・生活はどうでしたか？\n"
    "例：『今日就活した。大手企業調べは何から勉強すればいいかわからない』\n"
    "そのまま送ってOKです。"
)

PROFILE_QUERY_PHRASES = [
    "今の私はどんなですか",
    "今の私はどんな",
    "私ってどんな",
    "自分ってどんな",
]

# ==========
# Load env
# ==========
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

missing = [k for k, v in {
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}.items() if not v]
if missing:
    raise RuntimeError(f".env に必要な環境変数がありません: {', '.join(missing)}")

# ==========
# Init
# ==========
app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"api_version": "v1"},
)

# ==========
# DB helpers
# ==========
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        log_date TEXT NOT NULL,              -- YYYY-MM-DD (JST)
        user_text TEXT NOT NULL,
        ai_reply TEXT NOT NULL,
        model_used TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)
    conn.commit()
    conn.close()

def upsert_user(user_id: str):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?, ?)",
        (user_id, datetime.now(JST).isoformat())
    )
    conn.commit()
    conn.close()

def save_log(user_id: str, log_date: str, user_text: str, ai_reply: str, model_used: str | None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO daily_logs(user_id, log_date, user_text, ai_reply, model_used, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
    """, (user_id, log_date, user_text, ai_reply, model_used, datetime.now(JST).isoformat()))
    conn.commit()
    conn.close()

def get_recent_logs(user_id: str, limit: int = 20):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT log_date, user_text, ai_reply, model_used, created_at
        FROM daily_logs
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def list_all_users():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

# ==========
# LINE helpers
# ==========
def reply_text(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def push_text(user_id: str, text: str):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            )
        )

# ==========
# Gemini safe call (重要)
# ==========
def _extract_retry_seconds(msg: str) -> int | None:
    # "Please retry in 38.507s." / "retryDelay': '36s'" のどちらも拾う
    m = re.search(r"retry in ([0-9]+(\.[0-9]+)?)s", msg)
    if m:
        return int(float(m.group(1)))
    m = re.search(r"retryDelay[^\d]*([0-9]+)s", msg)
    if m:
        return int(m.group(1))
    return None

def generate_with_fallback(prompt: str, max_wait_seconds: int = 0):
    """
    - 429 なら別モデルにフォールバック
    - それでもダメなら例外を投げず (None, None, retry_sec) を返す
    """
    last_retry = None

    for model in MODEL_CANDIDATES:
        try:
            res = client.models.generate_content(model=model, contents=prompt)
            text = getattr(res, "text", None)
            if text:
                return text, model, None
            return "ごめん、今うまく文章を作れなかった…もう一回送って！", model, None

        except ClientError as e:
            # 429: クォータ超過
            status = getattr(e, "status_code", None)
            msg = str(e)
            if status == 429 or "RESOURCE_EXHAUSTED" in msg:
                retry_sec = _extract_retry_seconds(msg)
                last_retry = retry_sec

                # max_wait_seconds > 0 ならその範囲で待って再試行（webhookで待つのは基本おすすめしない）
                if max_wait_seconds and retry_sec and retry_sec <= max_wait_seconds:
                    time.sleep(retry_sec)
                    continue

                # 次のモデルへ
                continue

            # その他のエラーは抜ける（通信/設定/権限など）
            break

        except Exception:
            break

    return None, None, last_retry

# ==========
# Prompt builders
# ==========
def build_recent_text(rows):
    if not rows:
        return "(履歴なし)"
    txt = ""
    for r in reversed(rows):
        txt += f"- [{r['log_date']}] ユーザー: {r['user_text']}\n  AI: {r['ai_reply']}\n"
    return txt

def build_daily_prompt(user_message: str, rows):
    recent_text = build_recent_text(rows)
    return (
        "あなたは優しく現実的な就活・学習サポーターです。\n"
        "ユーザーの本日の報告を受けて、次の行動が進むように短く具体的に返してください。\n"
        "要件:\n"
        "- 1〜3文で。\n"
        "- 可能なら“質問を1つ”入れて次の会話につなげる。\n"
        "- 説教はしない。\n\n"
        "直近の履歴:\n"
        f"{recent_text}\n\n"
        "本日のユーザー発言:\n"
        f"{user_message}\n"
    )

def build_profile_prompt(rows):
    recent_text = ""
    if rows:
        for r in reversed(rows):
            recent_text += f"- [{r['log_date']}] {r['user_text']}\n"
    else:
        recent_text = "(ログなし)"

    return (
        "あなたはユーザーの成長を見守るコーチです。\n"
        "以下のログから、ユーザーの最近の状態を“人物像”として短くまとめてください。\n"
        "要件:\n"
        "- 日本語\n"
        "- 3〜5文\n"
        "- 1文目で性格/強み（例：向上心、継続力）\n"
        "- 2〜3文目で最近の忙しさ/課題\n"
        "- 最後に“優しい一言 + 次の一歩の提案”\n\n"
        "ログ:\n"
        f"{recent_text}\n"
    )

# ==========
# Webhook
# ==========
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # ★ここで落とさない（500回避）
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        # 例外が起きても 200 を返して LINE 側の再送ループを避ける
        print(f"[callback error] {e}")
        return "OK"

    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = getattr(event.source, "user_id", None)
    if not user_id:
        reply_text(event.reply_token, "ユーザーIDが取得できなかったよ。1:1トークで試してね。")
        return

    upsert_user(user_id)
    user_message = (event.message.text or "").strip()
    today_jst = datetime.now(JST).date().isoformat()

    # 「今の私はどんなですか？」系
    if any(p in user_message for p in PROFILE_QUERY_PHRASES):
        logs = get_recent_logs(user_id, limit=30)
        prompt = build_profile_prompt(logs)
        text, used_model, retry_sec = generate_with_fallback(prompt)

        if text is None:
            # クォータ超過時の定型返し（落ちない）
            wait = f"{retry_sec}秒" if retry_sec else "しばらく"
            reply_text(event.reply_token, f"ごめん、今AIの回数制限に当たってる…{wait}後にもう一回お願い！")
            return

        reply_text(event.reply_token, text)
        return

    # 通常：今日の報告
    logs = get_recent_logs(user_id, limit=10)
    prompt = build_daily_prompt(user_message, logs)
    text, used_model, retry_sec = generate_with_fallback(prompt)

    if text is None:
        # ★ここが超重要：429でも返信して会話が止まらないようにする
        wait = f"{retry_sec}秒" if retry_sec else "しばらく"
        fallback = (
            f"ごめん、今AIの回数制限に当たってる…{wait}後にもう一回送って！\n"
            "（先にメモだけ残すね：そのまま続き送ってOK）"
        )
        # 返信は定型、ログも保存（AI返答はfallbackとして保存）
        save_log(user_id, today_jst, user_message, fallback, None)
        reply_text(event.reply_token, fallback)
        return

    save_log(user_id, today_jst, user_message, text, used_model)
    reply_text(event.reply_token, text)

# ==========
# Daily 21:00 push job (Geminiは呼ばない)
# ==========
def nightly_checkin_job():
    users = list_all_users()
    for uid in users:
        try:
            push_text(uid, DAILY_QUESTION)
        except Exception as e:
            print(f"[push failed] user={uid} err={e}")

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=JST)
    trigger = CronTrigger(hour=21, minute=0)  # 毎日21:00 JST
    scheduler.add_job(nightly_checkin_job, trigger, id="nightly_checkin", replace_existing=True)
    scheduler.start()
    return scheduler

# ==========
# Main
# ==========
if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(port=5000, debug=True)