# MiraRec

Mirrativの配信を自動録画・クリップ管理するシステムです。

## 必要なもの

- Docker + Docker Compose
- Discordウェブフック URL（通知用）

## セットアップ

### 1. 設定ファイルを作成

```bash
cp .env.example .env
```

`.env` を開いて各項目を編集してください：

| 項目 | 説明 |
|------|------|
| `MYSQL_ROOT_PASSWORD` | MySQLのrootパスワード（任意の文字列） |
| `MYSQL_PASSWORD` | MySQLのユーザーパスワード（任意の文字列） |
| `ADMIN_USER` / `ADMIN_PASS` | Web UIの管理者ログイン情報 |
| `PUBLIC_URL` | 外部からアクセスできるURL（例: `https://example.com`） |
| `MENTAKO_USER_ID` | 録画対象のMirrativユーザーID |
| `GUEST_USER_IDS` | ゲストユーザーに表示するユーザーID（カンマ区切り） |
| `DISCORD_WEBHOOK_RECORDER` | 録画開始/終了の通知先DiscordウェブフックURL |
| `DISCORD_WEBHOOK_CLIP` | クリップ作成通知先DiscordウェブフックURL |

### 2. 起動

```bash
docker compose up -d
```

### 3. Web UIにアクセス

ブラウザで `http://localhost:3001`（または設定した`PORT`）を開く。

管理者でログインすると録画ターゲットの追加・設定ができます。

---

## 録画ターゲットの追加

Web UI → 「Targets」タブ → 「+ Add Target」でMirrativユーザーIDを追加します。

追加後、recorderコンテナが自動的にその配信の監視を開始します。

---

## データの場所

```
data/
├── nas/
│   ├── *.mp4          # 録画ファイル
│   ├── *.json         # 録画メタデータ
│   ├── clips/         # クリップ
│   ├── images/        # サムネイル
│   └── transcripts/   # 文字起こし
└── config/
    └── targets.json   # 録画ターゲット設定
```

---

## クリップコマンド

配信中にDiscordや配信コメントで以下のコマンドを送ると、自動でクリップが作成されDiscordに通知されます：

```
!切り抜き           # 直近60秒をクリップ
!切り抜き 30        # 直近30秒をクリップ
!切り抜き 90 タイトル  # 直近90秒、タイトル付き
```

---

## 停止・再起動

```bash
docker compose down        # 停止
docker compose restart     # 再起動
docker compose logs -f     # ログを見る
```

---

## 更新

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```
