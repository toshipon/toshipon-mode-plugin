---
name: verification-loop
description: PR を開く前や変更後に、リポジトリのスタック（Ruby / Go / Python / Node / CloudFormation / Terraform / Claude Code plugin）を判定して build・型・lint・テスト・秘密情報・diff の gate を回し、READY / NOT READY を 1 枚で返す統合検証。「PR 前に検証して」「verification-loop を回して」「push 前チェック」と言われたとき、または toshipon-mode の playbook が PR を開く直前に使う
---

# Verification loop

PR 作成前やコード変更後に、リポジトリに合った gate を順番に回す。gate は検証ではない。型チェックや lint や緑のテストは、それぞれ主張より狭いものを確認しているだけである。実物が動くことの証拠は別に取る（`create-verification-skill` で作った repo 固有の検証 skill など）。この skill が返すのは「PR を開いてよい状態か」であり、「正しく動く」ではない。

## Step 0. スタックを判定する

Step 5 と Step 6 は `origin/<default>` との diff を取る。先に default branch が手元にあることを確かめる。shallow clone や一時ディレクトリには無いことがある。

```bash
default_branch="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')"
[ -n "$default_branch" ] || default_branch=main
git rev-parse --verify "origin/$default_branch" >/dev/null 2>&1 || git fetch origin "$default_branch"
```

リポジトリのルートにあるファイルからスタックを判定する。複数に該当するなら、該当する gate をすべて回す。判定結果と回す gate を、最初に 1 行で述べる。リポジトリの `CLAUDE.md`、`Makefile`、CI 定義（`.github/workflows/*.yml` など）に検証コマンドが書かれていれば、下の一般コマンドよりそちらを優先する。CI と同じコマンドを回すのが最も安い。

| 判定ファイル | スタック | 回す gate |
|---|---|---|
| `Gemfile` | Ruby / Rails | `rubocop`、`rspec`（変更ファイルに対応する spec）、migration を含む場合は `bin/rails db:migrate:status` |
| `go.mod` | Go | `go build ./...`、`go vet ./...`、`go test ./...`、設定があれば `golangci-lint run` |
| `pyproject.toml` / `requirements.txt` | Python | `ruff check .`、設定があれば `pyright` か `mypy`、`pytest` |
| `package.json` | Node / TypeScript | `npm run build`、`npx tsc --noEmit`、`npm run lint`、`npm test` |
| `template.yaml` / `*.cfn.yml` / `cfn/` など CloudFormation テンプレート | CloudFormation | `cfn-lint`。変更したスタックは、検証環境で change set を作って差分を読む（`aws cloudformation create-change-set` → `describe-change-set`）。`deploy` と change set の実行は承認ゲートの対象 |
| `*.tf` | Terraform | `terraform fmt -check`、`terraform validate`、`terraform plan` の差分。`apply` は承認ゲートの対象 |
| `SKILL.md` / `.claude-plugin/` | Claude Code plugin | `claude plugin validate .`。リポジトリに独自の検証スクリプトがあればそれも |

判定できない場合は、検証コマンドをユーザーに 1 回だけ聞き、以降はそれを使う。

IaC の差分は、ファイルの diff ではなく**レンダリング後の差分**（change set、plan）で読む。テンプレートの diff が小さくても、パラメータやマクロの展開で実リソースの差分が大きくなることがある。置換（Replacement）や削除が含まれていたら、Issues に必ず書く。

## Step 1. Build

スタックの build コマンドを回す。失敗したらここで止め、修正を優先する。以降の gate は build が通ってから意味を持つ。

## Step 2. 型チェック

型を持つスタックだけ回す。エラー件数を記録する。既存のエラーが変更前からあったかどうかは、`git stash` せずに default branch で同じコマンドを回して比べる（`git worktree add` で trunk を並べる）。

## Step 3. Lint

自動修正できる指摘（`rubocop -a`、`ruff --fix`、`terraform fmt`）は直してから再実行する。自動修正の diff は commit 前に読み、lint が挙動を変えていないことを確かめる。

## Step 4. テスト

変更に対応するテストを回してから、スイート全体を回す。全体が長い（10 分超）場合は、変更ファイルに対応するテストだけを回し、全体は CI に任せると明記する。

Ruby では、変更した model / controller / job に対応する spec を `bundle exec rspec <path>` で回す。Go では `go test ./... -run <Pattern>` で絞ってから全体を回す。カバレッジの数値目標はリポジトリの規約に従う。規約が無ければ、変更した分岐がテストで踏まれているかを diff と突き合わせて述べる。

## Step 5. 秘密情報とデバッグ残骸

```bash
# 追加行だけを対象にする。既存の一致は別 issue
git diff origin/<default>...HEAD -U0 | grep '^+' | grep -nEi 'AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|gh[pous]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|AIza[0-9A-Za-z_-]{35}|xox[bp]-|api[_-]?key\s*[:=]|password\s*[:=]|secret\s*[:=]|BEGIN (RSA|OPENSSH) PRIVATE KEY'
git diff origin/<default>...HEAD -U0 | grep '^+' | grep -nE 'binding\.pry|byebug|debugger|console\.log\(|fmt\.Println\(|print\(.*DEBUG|TODO: remove'
```

一致が 1 件でもあれば NOT READY。秘密情報は commit 履歴からも消す。push 前なら `git commit --amend` か rebase で履歴から落とし、push 済みなら鍵のローテーションが必要だとユーザーに伝える。

## Step 6. Diff レビュー

```bash
git diff origin/<default>...HEAD --stat
git diff origin/<default>...HEAD
```

diff を全部読む。narrating comment、意図しないファイル（ロックファイルの巨大差分、生成物、`.env`）、無関係な整形が混ざっていないかを見る。混ざっていれば commit を分ける。

## 出力フォーマット

```
VERIFICATION REPORT
==================

Stack:     <判定結果。複数なら列挙>
Build:     PASS | FAIL | n/a
Types:     PASS | FAIL (X errors, trunk: Y) | n/a
Lint:      PASS | FAIL (X warnings) | n/a
Tests:     PASS | FAIL (X/Y passed) | PARTIAL (変更分のみ、全体は CI)
IaC diff:  PASS (置換・削除なし) | REVIEW (置換 X / 削除 Y) | n/a
Secrets:   PASS | FAIL (X hits)
Diff:      X files changed, 意図しないファイル: なし | <一覧>

Overall:   READY | NOT READY for PR

Issues to fix:
1. ...
```

`n/a` にした gate には理由を 1 語添える（`n/a (no type system)`）。PARTIAL は隠さず PARTIAL と書く。

## 使い方

```
verification-loop を回して
/verification-loop
```

toshipon-mode の Bug fix / Feature / Refactoring / Perf issue playbook は、PR を開く直前にこの skill を呼ぶ。
