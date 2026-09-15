# note-scheduler-sample

note連載「定期実行ジョブを見張るMacウィジェット」（全6回）のサンプルです。
定期実行スケジューラを、**このリポジトリの中だけで**動かせるようにまとめてあります。

**含まれるのはスケジューラ本体とジョブ定義だけです。** 連載で作った macOS アプリとウィジェット（UI）は
含みません。記事に載せているウィジェットの画像は、同梱のジョブをそのアプリで表示したものです。

## 何をするものか

`scheduler/dispatch.py` が cron 式の判定・依存関係の解決・並列実行・ログ出力を行います。
ジョブ定義は JSON で、`global/schedules/` と `projects/<name>/schedules/` に置きます。

ジョブの実行タイプは **`command` と `claude` の2つだけ**です。

| type | コスト | 同梱しているジョブ |
|---|---|---|
| `command` | 無料 | `draft-stats` / `draft-review` / `link-check` / `publish-check` / `notes-index` |
| `claude` | **課金** | `topic-digest`（既定で無効） |

`draft-review` は `command` の中からローカルLLM（ollama）を呼んでいます。
**スケジューラの機能ではなく、`command` でできることの応用です。**

`claude` のジョブは既定で `enabled: false` にしてあります。有効にすると API 利用料が発生します。

**`enabled: false` が止めるのは定期実行だけです。** `run` による手動実行は素通りするため、
`bash run.sh run global/topic-digest` は無効のままでも Claude を呼び、課金されます
（実測で1回あたり $0.07 程度）。`claude` のジョブを試すときは、この一発も課金対象だと
承知したうえで実行してください。

## 同梱しているジョブ

| ジョブ | 何をするか |
|---|---|
| `global/draft-stats` | 原稿の文字数・見出し数・画像プレースホルダの残数を数える |
| `global/draft-review` | 原稿をローカルLLMに読ませて講評させる。`draft-stats` の成功に連鎖する |
| `global/link-check` | 原稿内のリンク切れを探す |
| `global/publish-check` | 未解決の画像プレースホルダとローカル絶対パスを検出する。**見つかると失敗で終わる** |
| `demo/notes-index` | プロジェクトスコープのジョブ。スコープごとに実行時のディレクトリが変わることを確かめる |
| `global/topic-digest` | 直近のコミットから記事ネタを出す。**既定で無効** |

`publish-check` は、同梱の原稿にプレースホルダが残っているため**実行すると失敗します**。
失敗の見え方を確かめるためのもので、壊れているわけではありません。

`draft-review` は ollama が入っていない環境ではスキップして正常終了します。
モデルは環境変数 `SAMPLE_OLLAMA_MODEL` で変えられます（既定は `gemma4:e4b`）。

`link-check` は、**同梱の原稿にリンクが1本も無いため、常に「リンク切れはありません」で成功します。**
検出そのものを確かめたい場合は、`drafts/sample-article.md` に存在しないファイルへのリンク
（例: `[図](assets/none.png)`）を1行足してから実行してください。失敗して行が表示されます。

## 使い方

```bash
git clone <このリポジトリ>
cd note-scheduler-sample
bash run.sh list
```

```bash
bash run.sh run global/draft-stats
```

`run.sh` は本体に2つの設定を渡しているだけの薄いラッパです。

- `--config-dir` — ジョブ定義の読み先を、このリポジトリに向ける
- `CLAUDE_SCHEDULER_HOME` — ログ・ロック・一時停止の置き場を `.state/` に向ける

**スケジューラは `~/.claude/` 以下に一切書き込みません。** OS のスケジューラ（launchd 等）への
登録もしません。定期実行の体験は記事本文で説明しています。撤去は clone を削除するだけです。

ただし `claude` のジョブを実行した場合は、Claude Code 自身が `~/.claude/projects/` に
セッション記録（1回あたり数百KB）を残します。これはサンプルではなく Claude Code の動作です。
`command` のジョブだけを動かすかぎり、書き込みは `.state/` の中だけです。

## 動作確認環境

以下の環境で動作を確認しています。

| | 値 |
|---|---|
| macOS | 15.7.3（24G419）/ Apple Silicon |
| Python | 3.9.6（`/usr/bin/python3`、Command Line Tools 同梱）。標準ライブラリのみ使用 |
| bash | 3.2.57（macOS 同梱のもの） |
| ollama | 0.33.2 |
| モデル | `gemma4:e4b` — 8.0B / 量子化 Q4_K_M / コンテキスト 131,072 / **ディスク 9.6GB** / 要 ollama 0.20.0 以上 / Apache-2.0 |
| Claude Code | 2.1.270（`claude` のジョブのみ。要ログイン・**課金**） |

- **ollama が未導入の場合、`draft-review` はスキップして正常終了します。** モデルは 9.6GB あるので、
  試す場合はディスクの空きを確認してください
- `topic-digest` を試すには Claude Code のログインが必要です

## 本体について

`scheduler/dispatch.py` は別の非公開リポジトリで開発しているものを、
**コメントとdocstringを除去したうえで**コピーしています（先頭のライセンス表記3行を除く）。
処理内容は変更していません。
元のコミットは `scheduler/VERSION` に記録してあります。
このサンプルは記事に合わせて凍結したもので、本体の更新には追従しません。

本体が表示するメッセージには、開発元の構成を前提とした案内
（`setup-scheduler.sh` など、このサンプルに含まれないファイル名）が出ることがあります。
とくに `bash run.sh status` は「セットアップが必要です: bash setup-scheduler.sh install」と
表示して終了コード1で終わりますが、**このサンプルでは正常です。**
サンプルは OS のスケジューラに登録しないので、tick が来ないのが正しい状態です。

サンプルでの操作はすべて `run.sh` から行ってください。

## サポート

**無保証です。** 上記の動作確認環境以外での動作は確認していません。
個別の環境に対する対応は行いません。

## ライセンス

[PolyForm Internal Use License 1.0.0](LICENSE.md)

- 個人利用・社内での業務利用ともに可
- 改変可（手元で書き換えて試すことを想定しています）
- **再配布不可。** 製品やサービスに組み込んで社外へ提供する用途も対象外です
- 著作権者は Yuuki Reiya です

※ この README は Claude Code に書かせたものです。
