# Search notes

Search は、ユーザーがタイトルや本文のテキストでノートを見つけ、マッチしたノートを確認し、マッチなしと検索が使えない状態を区別できるようにする。

## Sub-features

- `search-open` は、サポートされる各ブラウザの入り口から検索を開く。
- `search-match` は、ノートのデータを変更せずにタイトルと本文のマッチを返す。
- `search-open-result` は結果をノートエディタで開く。
- `search-empty` は、マッチのないクエリに対して完全な empty state を示す。
- `search-clear` はクエリを取り除き、recent-notes view を復元する。
- `search-cli` はターミナルから同じマッチしたノートを返す。

## How to get to it (user POV)

- ブラウザツールバーの `Search` ボタンを選ぶ。
- 編集可能なフィールドの外にフォーカスがある状態でブラウザで `/` を押す。
- ターミナルで `notes search <query>` を実行する。

## Driving it with control-notes

Preconditions:

- Notes は `http://127.0.0.1:4173` で healthy である。
- 使い捨てのデータディレクトリには、本文が `Draft budget` の `Quarterly plan` が含まれている。
- `control-notes doctor` が期待される URL とデータディレクトリを報告する。

- **Toolbar entry.** `Search` ボタンを選ぶ。`control-notes browser click --role button --name "Search"` を実行する。`Search notes` という名前のダイアログが現れ、フォーカスはその searchbox にある。
- **Keyboard entry.** ダイアログを閉じ、ページにフォーカスし、`/` を押す。`control-notes browser press --key "/"` を実行する。同じダイアログが現れ、ページにはスラッシュが挿入されない。
- **Title match.** `quarterly` と入力する。`control-notes browser fill --role searchbox --name "Search notes" --value "quarterly"` を実行する。`Search results` の一覧に `Quarterly plan` が含まれ、`Grocery list` は含まれない。
- **Body match.** クエリを `budget` に置き換える。`control-notes browser fill --role searchbox --name "Search notes" --value "budget"` を実行する。結果の `Quarterly plan` が本文マッチの抜粋とともに表示され続ける。
- **Open result.** `Quarterly plan` を選ぶ。`control-notes browser click --role link --name "Quarterly plan"` を実行する。ダイアログが閉じ、エディタの見出しが `Quarterly plan` になる。
- **Empty state.** 検索を再度開き、`volcano` と入力する。`control-notes browser fill --role searchbox --name "Search notes" --value "volcano"` を実行する。検索完了後に `No matching notes` という名前の status が現れる。
- **Clear query.** `Clear search` を選ぶ。`control-notes browser click --role button --name "Clear search"` を実行する。searchbox が空になり、`Recent notes` の region が結果一覧を置き換える。
- **CLI match.** ターミナルから検索する。`control-notes cli -- notes search "quarterly" --format json` を実行する。exit code は `0` で、stdout にタイトルが `Quarterly plan` のオブジェクトが 1 つ含まれる。
- **CLI miss.** 存在しない値を検索する。`control-notes cli -- notes search "volcano" --format json` を実行する。exit code は `0` で、stdout は `[]` である。
- **Proof.** 結果が populated された状態を捕捉する。`control-notes browser snapshot --aria --path artifacts/search/results.aria.txt` と `control-notes browser screenshot --path artifacts/search/results.png` を実行する。両方の成果物が Notes、クエリ、`Quarterly plan` を示している。

## Gotchas

- エディタか searchbox にフォーカスがある状態で `/` を押すと、検索を開く代わりにテキストが挿入される。
- 結果は短い debounce の後に更新される。固定の sleep ではなく、結果一覧か empty status を待つ。
- Archived なノートは、ユーザーが `Include archived` を有効にしない限り除外される。
- CLI はデフォルトで人間が読みやすい出力をする。安定した assertion には `--format json` を使う。
- 結果を開くとブラウザの状態が変わる。別のクエリを証明する前に検索を開き直す。
