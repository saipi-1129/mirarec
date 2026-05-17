# MiraRec

Mirrativの配信を自動録画・クリップ管理するシステムです。

## 必要なもの

- Docker + Docker Compose
- Discordウェブフック URL（通知用、任意）

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/saipi-1129/mirarec.git
cd mirarec
```

### 2. 設定ファイルを作成

```bash
cp .env.example .env
```

`.env` を開いて各項目を編集してください：

| 項目 | 説明 |
|------|------|
| `MYSQL_ROOT_PASSWORD` | MySQLのrootパスワード（任意の文字列） |
| `MYSQL_PASSWORD` | MySQLのユーザーパスワード（任意の文字列） |
| `ADMIN_USER` / `ADMIN_PASS` | Web UIの管理者ログイン情報 |
| `PUBLIC_URL` | 外部からアクセスできるURL（例: `https://example.com`）。クリップ共有リンクに使用 |
| `GUEST_USER_IDS` | ゲストユーザーに表示するMirrativユーザーID（カンマ区切り、空欄で全非表示） |
| `DISCORD_WEBHOOK_RECORDER` | 録画開始/終了/エラー通知先DiscordウェブフックURL |
| `DISCORD_WEBHOOK_CLIP` | クリップ作成通知先DiscordウェブフックURL |
| `MENTAKO_USER_ID` | コメントから`!切り抜き`コマンドを拾うMirrativユーザーID |

### 3. 起動

```bash
docker compose up -d
```

### 4. Web UIにアクセス

ブラウザで `http://localhost:3001`（または設定した`PORT`）を開く。

管理者アカウントでログインすると、録画ターゲットの追加・設定ができます。

---

## 録画ターゲットの追加

Web UI → 「Targets」タブ → 「+ Add Target」でMirrativユーザーIDを追加します。

追加後、recorderコンテナが自動的にそのユーザーの配信を監視し、配信開始と同時に録画を開始します。

---

## 主な機能

- **自動録画** — 監視対象ユーザーが配信を開始すると自動で録画開始
- **Web UI** — 録画一覧・再生・削除・クリップ管理をブラウザで操作
- **クリップ** — 録画の任意の区間を切り抜き、Discordに自動投稿
- **文字起こし** — 録画をWhisperで文字起こし（要別途モデル設定）
- **Discord通知** — 録画開始/終了/エラー/クリップ作成をDiscordに通知
- **ゲストアクセス** — 特定ユーザーの録画のみ閲覧できるゲストモード

---

## クリップコマンド

配信中にMirrativコメントで以下のコマンドを送ると、自動でクリップが作成されDiscordに投稿されます（`MENTAKO_USER_ID` で指定したユーザーのコメントのみ有効）：

```
!切り抜き              # 直近60秒をクリップ
!切り抜き 30           # 直近30秒をクリップ
!切り抜き 90 タイトル  # 直近90秒、タイトル付き
```

---

## データの場所

```
data/
├── recordings/        # コンテナ内 /app/data にマウント
│   ├── *.mp4          # 録画ファイル
│   ├── *.json         # 録画メタデータ
│   ├── clips/         # クリップ
│   ├── images/        # サムネイル
│   └── transcripts/   # 文字起こし結果
└── config/
    └── targets.json   # 録画ターゲット設定
```

---

## 停止・再起動

```bash
docker compose down        # 停止
docker compose restart     # 再起動
docker compose logs -f     # 全コンテナのログを見る
docker compose logs -f web # Webサーバーのログのみ
```

---

## 更新

```bash
docker compose down
git pull
docker compose build --no-cache
docker compose up -d
```
