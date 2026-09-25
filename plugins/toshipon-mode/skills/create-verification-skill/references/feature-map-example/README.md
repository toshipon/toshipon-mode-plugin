# Notes verification map

このディレクトリは、Notes のユーザー向け挙動を検証するために維持される情報源である。アプリを駆動する前に index を読み、対応する feature file をレシピとして使う。

## Baseline preconditions

- 使い捨てのデータディレクトリを使って `http://127.0.0.1:4173` で Notes を起動する。
- 並行する run が状態を共有しないよう `NOTES_DATA_DIR=/tmp/notes-verify-$RUN_ID` を設定する。
- `Quarterly plan` と `Grocery list` というタイトルのノートを seed する。
- `control-notes` と `notes` CLI を `PATH` に置く。
- `control-notes doctor` を実行し、期待される URL、データディレクトリ、ビルドリビジョンを求める。
- この verification run が起動していないインスタンスは絶対に駆動しない。

## Driving conventions

- 各レシピは、そのレシピの precondition が別のことを言わない限り baseline の状態から始める。
- CSS selector や DOM の位置より ARIA role と accessible name を優先する。
- すべてのコマンドを文字通りのものとして扱う。引用された名前やフラグは変えない。
- ブラウザの操作は `control-notes browser` 経由で実行する。
- ターミナルの操作は `control-notes cli -- <command>` 経由で実行する。
- mutation の後は seed データを復元する。cleanup 中に証明の成果物を削除しない。

## Proof and skip reporting

- ユーザーのアクションと結果として生じた状態を捕捉する。最終画面だけではない。
- UI の証明には ARIA snapshot と、アプリの identity が見えるスクリーンショットを含める。
- CLI の証明には、コマンド、stdout、stderr、exit code を含める。
- mutation の証明には、保存された値の read-only な second view を含める。
- すべての成果物に、使用した feature ID と入り口を記録する。
- 到達できない path は、試みたコマンドと満たされなかった precondition とともに報告する。
- スキップした入り口を、別の path を経由して検証済みと報告しない。

## Feature entry contract

各 feature file は H1 タイトルと、ユーザーに見える挙動を説明する 1 段落から始まる。続いて、次の順序で正確に 4 つの H2 セクションを使う。

1. `Sub-features` は各挙動を 1 行で説明する短い ID を列挙する。
2. `How to get to it (user POV)` はすべてのユーザーの入り口を列挙する。
3. `Driving it with <harness>` は `Preconditions:` から始まり、各ユーザーアクションを正確なコマンドと観測可能な結果と対にした labeled bullet を使う。
4. `Gotchas` は verification run を無駄にしたり無効にしたりし得る罠を列挙する。

map からは実装の詳細を外す。ユーザーの path、安定したハンドル、必要な状態、コマンド、観測可能な証明だけを名指しする。

## Features

- [Create a note](./create-note.md) はブラウザと CLI での作成、キャンセル、永続化、cleanup をカバーする。
- [Search notes](./search.md) はツールバー、キーボード、CLI での検索を、マッチ、空、クリアの各状態でカバーする。
