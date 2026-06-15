# AgentBoard Collectors

[English](./README.md) | [简体中文](./README.zh-CN.md) | **日本語** | [한국어](./README.ko.md) | [Português (BR)](./README.pt-BR.md)

AI コーディング使用量のためのオープンソース・ローカルコレクター。

AgentBoard コレクターは、AI コーディングツールがすでにあなたのマシンに保存しているログをスキャンし、ローカルで使用量統計に集計して、**メタデータのみ**を [AgentBoard](https://agentboard.cc) に同期します。プロンプト、モデルの応答、ソースコード、diff、ファイル内容、ファイルパス、ターミナル出力をアップロードすることは一切ありません。

ローカルログに触れるすべてのコードはこのリポジトリにあり、**外部依存ゼロ**の素の Python で書かれています。何がスキャンされ、何が送信されるのか、一行ずつ確認できます。

## 対応ソース

| ソース | ステータス | スキャンされるローカルデータ |
| --- | --- | --- |
| Claude Code | ✅ 対応済み | `~/.claude/projects`、`$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ 対応済み | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ 対応済み | `~/.codex/sessions`、`~/.codex/archived_sessions`、`$CODEX_HOME`、`%APPDATA%/codex` |
| Gemini | ✅ 対応済み | `~/.gemini/tmp`、`$GEMINI_CLI_HOME/tmp` |
| OpenCode | ✅ 対応済み | `~/.local/share/opencode`、`$OPENCODE_HOME`、`$OPENCODE_DB` |
| OpenClaw | ✅ 対応済み | `~/.openclaw`、`$OPENCLAW_HOME`、`$OPENCLAW_DIR` |

**ソースの集約方法。** 各コレクターは特定のクライアントではなく、ツールのローカルセッションストア(上記のパス)を読み取ります — そのため、CLI に限らず、セッションをこれらのパスに書き込むクライアントはすべて収集対象になります。AgentBoard のリーダーボードでは、5 つのバッジに集約されます:**Claude**(Claude Code + Cowork)、**Codex**、**Gemini**、**OpenCode**、**OpenClaw**。

ソースごとの詳細: [docs/supported-sources.md](./docs/supported-sources.md)

## アップロードされるもの

AgentBoard がアップロードするのは**集計済みの使用量メタデータのみ**です：

- 日付、プレフィックス付きセッション id（例 `claude:<id>`）、ソース名
- デバイス名（サニタイズ済みホスト名。`$AGENTBOARD_DEVICE_NAME` で上書き可能）とプラットフォーム
- アクティブなコーディング時間、アクティブ時間ウィンドウ（タイムスタンプのみ）
- トークン数：input、output、キャッシュ読み取り、キャッシュ作成、思考、プロバイダー合計
- メッセージ数、ツール呼び出し数、上位ツールの**名前**と回数
- 追加・削除行数（編集系ツールからのカウント）
- 関与したプロジェクト数とファイル数 ——**数のみ**で、名前やパスは送信されません
- スキル使用状況（Claude Code）：公開 `SKILL.md` メタデータ由来のスキル名/キーと使用回数
- コレクターのバージョン

## マシンから出ていかないもの

- ❌ プロンプトとアシスタントの応答
- ❌ ソースコード、diff、ファイル内容
- ❌ ファイルパスとプロジェクトパス（アップロードされるのは数のみ）
- ❌ ターミナル出力と環境変数
- ❌ ユーザー名、メールアドレス、生のマシン識別子

フィールドごとの完全なリファレンス: [docs/data-fields.md](./docs/data-fields.md) · プライバシーポリシー: [PRIVACY.md](./PRIVACY.md)

## インストール

macOS / Linux:

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows (PowerShell):

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

インストーラーはこれらのコレクターを `~/.agentboard/` にコピーし、バックグラウンド同期（macOS は launchd、Windows はタスクスケジューラ）を登録し、Claude Code のセッション終了イベントにフックします。インストール後にリモート実行されるものはありません。

## まずローカルで試す

すべてのコレクターには `--summary` モードがあり、ログをスキャンして集計 JSON を表示します。**何もアップロードされません**。インストール前に実行して、AgentBoard が受け取る内容を自分の目で確認できます：

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## 仕組み

1. **フック** — Claude Code の `Stop` イベントが `hook.sh` をトリガーし、終了したセッションのコレクターを起動します（同一セッションは 5 分に 1 回までにスロットリング）。
2. **デーモン** — 軽量なバックグラウンドプロセスが 180 秒ごとに再スキャンし、Codex や Gemini など他ツールの使用量も同期され続けます。
3. **ローカル集計** — コレクターは JSONL/JSON/SQLite のセッションログを日別・セッション別の統計に解析します。解析は完全にあなたのマシン上で行われます。
4. **メタデータ同期** — 集計結果は `~/.agentboard/config.json` のデバイストークンを使って AgentBoard API に POST されます。サーバーが保存するのはトークンの**ハッシュ**のみで、生のトークンは保存しません。

## トークンの集計方法

ツールによってトークンの報告方法は異なります。AgentBoard はプロバイダー自身の合計値と完全な内訳の両方を保持するため、リーダーボードの数字は公式の使用量ダッシュボードと一致します：

- **Claude Code / Cowork / OpenCode / OpenClaw** — キャッシュトークンは独立フィールド。`provider_total = input + output + cache_read + cache_creation`。
- **Codex** — 報告される input にはキャッシュトークンがすでに含まれます。`provider_total = input + output`。キャッシュフィールドは内訳表示専用で、二重計上はしません。
- **Gemini** — プロバイダーの `totalTokenCount` をそのまま使用。Gemini はキャッシュ作成を報告しません。

すべてのソースで `non_cache_total = provider_total − cache_read − cache_creation` が利用でき、キャッシュを除いた比較が可能です。完全な仕様: [docs/token-accounting.md](./docs/token-accounting.md)

## AgentBoard ではないもの

AgentBoard は課金メーターではなく、暗号学的な作業証明でもありません。ローカルの AI ツールログは所有者が編集でき、トークンデータの公開方法もツールごとに異なります。AgentBoard が目指すのは、透明でソースを区別した使用量分析 — 個人の統計、チームの可視性、トレンドです。

## 開発

コレクターは標準ライブラリのみを使う独立した Python 3 スクリプトで、`pip install` は不要です。各スクリプトは一読で監査できるよう、意図的に自己完結型になっています。

```bash
python3 collectors/collect.py --summary            # 実ログに対するドライラン
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # トークン集計の詳細調査
```

Issue や PR を歓迎します — [CONTRIBUTING.md](./CONTRIBUTING.md) をご覧ください。完全なアンインストールは [UNINSTALL.md](./UNINSTALL.md) へ。

## ライセンス

[MIT](./LICENSE)
