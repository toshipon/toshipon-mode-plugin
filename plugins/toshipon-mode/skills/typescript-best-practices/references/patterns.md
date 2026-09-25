# TypeScript patterns

`SKILL.md` の各ルールに対応するコード例。根底にある原則は言語非依存であり、boundary 系のルールは ${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/references/principles/boundary-discipline.md を参照。

## Branded types

プリミティブ型にブランドを付け、混同できないようにする。生成時に一度だけ検証し、下流のコードはその型を信頼する。

```ts
type AgentId = string & { readonly __brand: "AgentId" };

function parseAgentId(input: string): AgentId {
  if (!isUUID(input)) throw new Error(`Invalid agent id: ${input}`);
  return input as AgentId;
}

function focusAgent(id: AgentId): void {
  /* input is trusted */
}
```

`readonly __brand: 'X'` の形に合わせる。新しい独自の慣習を作らない。

## Discriminated unions

「待てよ、この組み合わせは本当に起こり得るのか」とバグから問い直すことになったら、その型は緩すぎる。バリアントをリテラルの判別子でモデル化する。すべてのバリアントが同じフィールド名を共有し、それぞれのバリアントの値は一意である。これにより、ありえない組み合わせは表現できなくなる。

```ts
// Don't. Boolean + optionals lets contradictory states exist.
type DiffState = { loading: boolean; diff?: GitDiff; error?: string };

// Do. Only valid states exist.
type DiffState =
  | { kind: "loading" }
  | { kind: "ready"; diff: GitDiff }
  | { kind: "error"; error: string };
```

判別子の名前は 1 つ（`kind`、`type`、`tag` のいずれか）に決め、それを守る。

## Constructive modeling

緩い型をランタイムチェックで制限するのではなく、すべて合法なパーツから型を組み立てる。足すほうが引くより簡単である。

可変長タプルによる非空の表現:

```ts
type NonEmpty<T> = [T, ...T[]];

// Don't: T[] plus a length check every caller must repeat
function pickWinner(entries: string[]): string {
  if (entries.length === 0) throw new Error("no entries");
  return entries[Math.floor(Math.random() * entries.length)];
}

// Do: an empty value of the type can't exist
function pickWinner(entries: NonEmpty<string>): string {
  return entries[Math.floor(Math.random() * entries.length)];
}
```

素の `T[]` が渡ってくる場所では、一度だけ guard で絞り込む。そうすればその事実が型として運ばれる。

```ts
const isNonEmpty = <T>(arr: T[]): arr is NonEmpty<T> => arr.length > 0;
```

ペアとしての偶数長。TypeScript には精緻化型がない（`arr.length % 2 === 0` を型レベルで表す方法はない）が、それは必要ない。

```ts
type Pairs<T> = [T, T][];
```

start と duration による時間範囲の表現:

```ts
// Don't: a comment holds the invariant
type TimeRange = { start: Date; end: Date }; // start <= end

// Do: a negative range can't be written; derive end when needed
type TimeRange = { start: Date; durationMs: number };
```

`durationMs` は素の number のままにする。ブランドを付けるのは（Branded types の項の通り）生の数値が duration の代わりに渡ってしまう可能性があるときだけで、反射的にやらない。`Pairs<T>` は、与えた解釈のもとでは偶数長リストであり、それは `{ start, durationMs }` が範囲であるのと同じ考え方である。不正な状態を構築不可能にする表現を選び、その上に必要な読み方（`pairs.flat()`、`rangeEnd()` ヘルパーなど）を用意する。

## Simplest total type

すべてを強化する必要はない。そのすべての操作が全域関数であるなら `T[]` のままにする。

```ts
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0); // [] is 0, fine
```

緩い型が利用箇所で嘘を強いるなら、そこで強化する。その兆候は `!`、`arr[0] as T`、「起こり得ないはず」の throw である。

```ts
// Don't: partiality smuggled past the compiler
function newestSession(sessions: Session[]): Session {
  return sessions.at(0)!;
}

// Do: strengthen the input; the assertion disappears
function newestSession(sessions: NonEmpty<Session>): Session {
  return sessions[0];
}
```

戻り値を `Session | undefined` に弱めるのも、もう 1 つの全域なシグネチャである。どちらの方法でも、空の場合の扱いは、空が何を意味するかを知っている唯一の場所である呼び出し側に落ち着く。

## `unknown` over `any`

`any` は触れる箇所すべてで型チェックを無効化する。外部データは常に `unknown` とし、使う前に絞り込む。

```ts
// Don't
function handle(input: any) {
  return input.foo.bar;
}

// Do
function handle(input: unknown) {
  if (typeof input === "object" && input !== null && "foo" in input) {
    // narrowed; compiler verifies access
  }
}
```

外部の情報源には、RPC のペイロード、`JSON.parse`、`postMessage`、IPC、ファイルの内容、環境変数、DB の結果などが含まれる。

## No `as` casts

すべての `as` は潜在的なランタイムクラッシュである。型システムがその主張を検証した後にのみキャストする。

```ts
// Don't
const user = data as User;

// Do. Earn the cast at the boundary.
function parseUser(data: unknown): User {
  if (typeof data !== "object" || data === null) {
    throw new Error("expected object");
  }
  if (!("id" in data) || typeof (data as Record<string, unknown>).id !== "string") {
    throw new Error("expected id");
  }
  // ... validate all fields
  return data as User; // OK, earned cast after full validation
}
```

既存コードから `as` をリファクタリングして取り除くときは、TypeScript が推論できない理由を突き止める。

- 判別子がない: 追加して discriminated union に切り替える。
- 元の型が広すぎる（例: `Record<string, unknown>`）: それを絞り込む。
- 境界に型が付いていない: パース関数やスキーマを追加する。
- 本質的に表現不可能: branded type か `satisfies` を使う。

## Narrowing hierarchy

優先度の高い順から最終手段まで:

1. **Discriminated union switch / if。** コンパイラが自動的に絞り込む。
2. **`in` 演算子。** `"key" in obj` はそのキーを含むバリアントに絞り込む。
3. **`typeof` / `instanceof`。** プリミティブとクラスインスタンス向け。
4. **ユーザー定義の type guard。** 上記だけでは足りない場合。
5. **`as` キャスト。** 検証の後にのみ。

```ts
function area(s: Shape): number {
  if ("radius" in s) return Math.PI * s.radius ** 2; // narrowed to circle
  return s.width * s.height; // narrowed to rect
}
```

## Type guards

guard は主張を実際に検証しなければならない。嘘をつく guard は `as` より悪い。バグが「安全」と主張する名前の裏に隠れるからである。

```ts
function isCircle(s: Shape): s is Shape & { kind: "circle" } {
  return s.kind === "circle";
}
```

可能なら判別子による絞り込みを優先する。guard は読者が追わなければならない層を 1 つ増やす。

## Exhaustiveness

default 節で、判別子を `never` 型のローカル変数に代入する。新しいバリアントが未処理のまま追加されると、コンパイラがエラーを出す。

```ts
// Value-returning switch
function area(s: Shape): number {
  switch (s.kind) {
    case "circle":
      return Math.PI * s.radius ** 2;
    case "rect":
      return s.width * s.height;
    default: {
      const _exhaustive: never = s;
      return _exhaustive;
    }
  }
}

// Void switch
function handle(s: Shape): void {
  switch (s.kind) {
    case "circle":
      drawCircle(s);
      break;
    case "rect":
      drawRect(s);
      break;
    default: {
      const _exhaustive: never = s;
      void _exhaustive;
    }
  }
}
```

値を返す switch では return 形式、文としての switch では void 形式を使う。

## `satisfies` over `as`

`satisfies` はリテラル型を広げずに検証する。

```ts
// Don't. Widens, loses literal types.
const config = { theme: "dark", cols: 3 } as Config;

// Do. Validates AND preserves literal types.
const config = { theme: "dark", cols: 3 } satisfies Config;
// config.theme is "dark" (literal), not string
```

## Boundary validation

データが境界を越えて入ってくる場所で一度だけ検証し、内部では型を信頼する。Boundary Discipline principle（${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/references/principles/boundary-discipline.md）を参照。

- **ワイヤーフォーマット**（proto、JSON-RPC）: `ignoreUnknownFields` でパースし、前方互換な変更で古いクライアントが壊れないようにする。
- **永続化された JSON:** バージョン付きの blob とし、パースを try/catch で囲む。
- **呼び出しチェーンの奥深くで再検証しない。**

## Schema-derived types

`.proto`、OpenAPI spec、GraphQL schema、DB マイグレーションがすでに形を定義している場合は、それを複製せず生成された型から派生させる。

```ts
// Don't. Duplicate shape, drifts when the schema changes.
type CheckSummary = {
  totalCount: number;
  checks: { name: string; status: string }[];
};
function renderChecks(s: CheckSummary) {
  /* ... */
}

// Do. Derive from the generated schema type.
import type { ChecksMessage } from "<generated module>";
function renderChecks(s: Pick<ChecksMessage, "totalCount" | "checks">) {
  /* ... */
}
```

新しい interface を書く前に `Pick`、`Omit`、`Parameters`、`ReturnType`、`Awaited`、`typeof` を検討する。

## Object args

```ts
// Don't. Swap two args, still compiles.
openFile(uri, {
  startLineNumber: 10,
  startColumn: 1,
  endLineNumber: 10,
  endColumn: 1,
});

// Do. Order-independent, self-documenting.
openFile({
  uri,
  selection: {
    startLineNumber: 10,
    startColumn: 1,
    endLineNumber: 10,
    endColumn: 1,
  },
});
```

ホットパス（フレームごとの render、トークナイザ、パーサ、アロケーションコストが問題になるタイトなループ内すべて）では省略する。
