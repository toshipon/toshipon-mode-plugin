---
name: verification-loop
description: PR前の統合検証（ビルド→型チェック→lint→テスト→セキュリティ）
---

# Verification Loop

PR 作成前やコード変更後の統合検証システム。

## 使用タイミング

- 機能実装完了後
- PR 作成前
- リファクタリング後

## 検証フェーズ

### Phase 1: ビルド検証

```bash
npm run build 2>&1 | tail -20
# または
pnpm build 2>&1 | tail -20
```

ビルド失敗時は STOP。修正を優先。

### Phase 2: 型チェック

```bash
# TypeScript
npx tsc --noEmit 2>&1 | head -30

# Python
pyright . 2>&1 | head -30
```

### Phase 3: Lint チェック

```bash
# JavaScript/TypeScript
npm run lint 2>&1 | head -30

# Python
ruff check . 2>&1 | head -30
```

### Phase 4: テスト実行

```bash
npm run test -- --coverage 2>&1 | tail -50
```

目標: 80% 以上のカバレッジ

### Phase 5: セキュリティスキャン

```bash
# シークレット検出
grep -rn "sk-\|api_key\|password" --include="*.ts" src/ 2>/dev/null | head -10

# デバッグコード検出
grep -rn "console.log\|debugger" --include="*.ts" src/ 2>/dev/null | head -10
```

### Phase 6: Diff レビュー

```bash
git diff --stat
git diff HEAD~1 --name-only
```

## 出力フォーマット

```
VERIFICATION REPORT
==================

Build:     [PASS/FAIL]
Types:     [PASS/FAIL] (X errors)
Lint:      [PASS/FAIL] (X warnings)
Tests:     [PASS/FAIL] (X/Y passed, Z% coverage)
Security:  [PASS/FAIL] (X issues)
Diff:      [X files changed]

Overall:   [READY/NOT READY] for PR

Issues to Fix:
1. ...
2. ...
```

## 呼び出し方

```
/verification-loop を実行して
```

または

```
PR 作成前に検証を実行して
```
