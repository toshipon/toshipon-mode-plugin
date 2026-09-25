---
name: typescript-best-practices
description: TypeScript のベストプラクティス。.ts / .tsx ファイルを読み書きする時に適用する
---

# TypeScript best practices

型チェッカーを証明支援系として扱う。ありえない状態を表現不可能にする。このスキルは、その規律を TypeScript の構文に落とし込む。

| Rule | Summary |
|------|---------|
| Discriminated unions | `kind` リテラルの判別子でバリアントをモデル化し、ありえない状態を表現できないようにする。オプショナルフィールドの寄せ集めにしない。 |
| Branded types | プリミティブ型に `& { readonly __brand: "X" }` でブランドを付け、混同できないようにする。生成時に一度だけ検証する。 |
| Constructive modeling | 不正な値をそもそも構築できない形に組み立てる。空でないことを表すなら `[T, ...T[]]`、偶数長を表すなら `[T, T][]`、範囲を表すなら `start` と `duration` の組。ランタイムガードではなく、精緻化型を願うことでもない。 |
| Simplest total type | すべての操作が全域関数であり続ける限り `T[]` のままにする。緩い型が `!`、キャスト、「起こり得ないはず」の throw を強いる箇所でのみ `NonEmpty<T>` に強化する。 |
| `unknown` over `any` | 外部データは `unknown` とする。`any` は触れる箇所すべてで型チェックを無効化する。 |
| No `as` casts | すべての `as` はランタイムクラッシュの予備軍である。検証の後にのみキャストする。 |
| Narrowing hierarchy | 判別子の switch > `in` 演算子 > `typeof`/`instanceof` > ユーザー定義の type guard > `as` の順で優先する。 |
| Type guards | 主張を実際に検証しなければならない。嘘をつく guard は `as` より悪い。バグが「安全」と主張する名前の裏に隠れるからである。`isX` や `hasX` と命名する。 |
| Exhaustiveness | default 節に `const _exhaustive: never = x;` をインラインで置き、新しいバリアントが追加されたときにコンパイラがエラーを出すようにする。 |
| `satisfies` over `as` | リテラル型を広げずに値を検証する。 |
| Boundary validation | データが境界を越えて入ってくる場所で、名前付きのドメイン型にパースする。`Record<string, unknown>`（どう書かれていても）はそのパース地点で止める。内部では型を信頼する。Boundary Discipline principle（${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/references/principles/boundary-discipline.md）を参照。 |
| Schema-derived types | 新しい interface を宣言する前に `Pick`/`Omit`/`Parameters`/`ReturnType`/`Awaited`/`typeof` を検討する。 |
| Object args | 位置引数ではなくオブジェクトを渡し、引数の順序が自己説明的になるようにする。ホットパス（フレームごとの render、トークナイザ、パーサ）では省略する。 |
| Real tests | 実行できるものをモックしない。leak・disposable チェックを備えたフレームワーク本来のテストプリミティブを優先し、UI は実際に動くビルドで検証する。ローカルで実行できないものだけをモックする。 |
| Structured telemetry | `console.log` を出荷コードに残さず、id からデバッグできるだけの文脈を持つ構造化ロガーの診断情報を優先する。 |

Examples: `references/patterns.md`.
