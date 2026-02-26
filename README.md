# LINE Career Check-in Bot (Gemini)

LINE上で動く就活・学習支援チャットボットです。毎日21:00(JST)に「今日どうだった？」と自動で問いかけ、返信内容をSQLiteに履歴として保存します。Gemini APIが直近ログを参照し、次の行動につながる短い助言や質問（1〜3文）を返します。さらに「今の私はどんなですか？」で最近の傾向を要約し、強み・忙しさ・課題と次の一歩を提示します。429（クォータ超過）時もフォールバックや定型返信で落ちない設計です。

## Features
- Daily push at 21:00 JST
- Log storage with SQLite (local)
- Gemini-based replies using recent logs
- Profile summary: 「今の私はどんなですか？」
- 429 quota handling (fallback / safe reply)

## Tech Stack
Python / Flask, LINE Messaging API v3, Google Gemini API (google-genai), APScheduler, SQLite

## Setup
```bash
pip install -r requirements.txt