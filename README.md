# MiraRec

Mirrativの配信を自動録画・クリップ管理するDockerシステムです。

## 必要なもの

- Docker + Docker Compose

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

基本的には `.env` の編集は不要です。MySQLのパスワードだけ変更することを推奨します：

```
MYSQL_ROOT_PASSWORD=任意のパスワード
MYSQL_PASSWORD=任意のパスワード
```

### 3. 起動

```bash
docker compose up -d
```

### 4. セットアップウィザードで初期設定

ブラウザで `http://localhost:3001` を開くと、初回セットアップウィザードが表示されます。

| ステップ | 内容 |
|----------|------|
| 1. データベース | MySQLを使用するか、ファイル保存のみにするかを選択 |
| 2. 管理者アカウント | ログインに使うユーザー名・パスワードを設定 |
| 3. 通知設定 | Discord Webhook URL・公開URL（任意） |
| 4. ゲスト設定 | ゲストに見せるMirrativユーザーID（任意） |

完了後、ログイン画面が表示されます。

> **MySQLを使わない場合**: コメント履歴はJSONファイルに保存されます。録画・クリップ・文字起こしは両方とも利用できます。

---

## 録画ターゲットの追加

Web UI → 「Targets」タブ → 「+ Add Target」でMirrativユーザーIDを追加します。

追加後、recorderコンテナが自動的にそのユーザーの配信を監視し、配信開始と同時に録画を開始します。

---

## 主な機能

- **自動録画** — 監視対象ユーザーが配信を開始すると自動で録画開始
- **Web UI** — 録画一覧・再生・削除・クリップ管理をブラウザで操作
- **クリップ** — 録画の任意の区間を切り抜き、Discordに自動投稿
- **文字起こし** — 録画をWhisperで文字起こし
- **Discord通知** — 録画開始/終了/エラー/クリップ作成をDiscordに通知
- **ゲストアクセス** — 特定ユーザーの録画のみ閲覧できるゲストモード

---

## クリップコマンド

配信中にMirrativコメントで以下のコマンドを送ると、自動でクリップが作成されDiscordに投稿されます（セットアップで設定した `CLIP_USER_ID` のコメントのみ有効）：

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
    ├── targets.json       # 録画ターゲット設定
    └── server_config.json # セットアップウィザードで保存した設定
```

---

## 停止・再起動

```bash
docker compose down        # 停止
docker compose restart     # 再起動
docker compose logs -f     # 全コンテナのログを見る
docker compose logs -f web      # Webサーバーのログのみ
docker compose logs -f recorder # 録画コンテナのログのみ
```

---

## 更新

```bash
docker compose down
git pull
docker compose build --no-cache
docker compose up -d
```

---

## パスワードを忘れた場合

```bash
docker compose exec web rm /app/config/server_config.json
docker compose restart web
```

ブラウザで `http://localhost:3001` を開くとセットアップウィザードが再表示されます。
