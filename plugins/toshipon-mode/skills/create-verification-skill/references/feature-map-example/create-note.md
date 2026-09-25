# Create a note

Create note は、ユーザーがブラウザまたは CLI からタイトル付きのノートを保存し、未完成の draft をキャンセルし、別のユーザー向け view から保存されたノートを確認できるようにする。

## Sub-features

- `create-open` は各ブラウザの入り口から空のエディタを開く。
- `create-save` はタイトルと本文を永続化する。
- `create-cancel` は未完成のブラウザの draft を破棄する。
- `create-cli` はターミナルから同じ形のノートを作成する。

## How to get to it (user POV)

- ブラウザツールバーの `New note` ボタンを選ぶ。
- 編集可能なフィールドの外にフォーカスがある状態でブラウザで `n` を押す。
- ターミナルで `notes create --title <title> --body <body>` を実行する。

## Driving it with control-notes

Preconditions:

- Notes は `http://127.0.0.1:4173` で healthy である。
- `Release checklist` というタイトルのノートはまだない。
- `control-notes doctor` が期待される URL と使い捨てのデータディレクトリを報告する。

- **Open editor.** `New note` を選ぶ。`control-notes browser click --role button --name "New note"` を実行する。`Note editor` という名前のフォームが現れ、フォーカスは `Title` の textbox にある。
- **Enter content.** タイトルと本文を入力する。`control-notes browser fill --role textbox --name "Title" --value "Release checklist"` と `control-notes browser fill --role textbox --name "Body" --value "Tag and publish"` を実行する。`Save note` ボタンが有効になる。
- **Save note.** `Save note` を選ぶ。`control-notes browser click --role button --name "Save note"` を実行する。`Note saved` という名前の status が現れ、見出しが `Release checklist` になる。
- **Confirm persistence.** ノート一覧に戻ってノートを再度開く。`control-notes browser click --role link --name "All notes"` と `control-notes browser click --role link --name "Release checklist"` を実行する。エディタに両方の保存済みの値が表示される。
- **Cancel draft.** 新しいノートを開き、`Discard me` と入力し、`Cancel` を選ぶ。`control-notes browser click --role button --name "New note"`、`control-notes browser fill --role textbox --name "Title" --value "Discard me"`、`control-notes browser click --role button --name "Cancel"` を実行する。ノート一覧に戻り、`Discard me` へのリンクはない。
- **CLI entry.** 2 つ目のノートを作成する。`control-notes cli -- notes create --title "CLI note" --body "Created from terminal" --format json` を実行する。exit code は `0` で、stdout に新しいノートの ID とタイトルが含まれる。
- **Proof.** `All notes` から保存済みの両方のノートを再度開く。`control-notes browser snapshot --aria --path artifacts/create-note/list.aria.txt` と `control-notes browser screenshot --path artifacts/create-note/list.png` を実行する。成果物には `Release checklist` と `CLI note` が示される。

## Gotchas

- textbox にフォーカスがある状態で `n` を押すと、新しいエディタを開く代わりに文字が入力される。
- タイトルは保存時に trim される。draft の入力値ではなく、レンダリングされたタイトルを assert する。
- save の status だけでは証明として不十分である。一覧からノートを開き直す。
- fixture の cleanup で `Release checklist` と `CLI note` は削除するが、その証明の成果物は残す。
