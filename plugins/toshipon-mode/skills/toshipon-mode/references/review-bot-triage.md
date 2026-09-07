# Review bot triage

自動 PR レビュー bot や agentic security review が PR にコメントしたときに、この reference を使う。目的は review bot をデフォルトで無視することではない。すべてのコメントを必須のコード変更として扱うのをやめることが目的である。

## Decision rubric

行動する前に、review-bot の各スレッドを分類する。

- `fix`: コメントが、もっともらしい正しさ・セキュリティ・プライバシー・データ損失・認証・課金・移行・冪等性・レース・出荷済み挙動の問題を指摘している。それを最も下流の owning PR で修正し、commit SHA を添えて返信し、スレッドを resolve する。
- `dismiss`: コメントが文書化された低リスクのノイズパターンに合致し、現在のコード・文脈がその懸念にコード変更が不要であることを証明している。短い理由とともに返信し、スレッドを resolve する。
- `ask`: コメントが新規、高severity、セキュリティ/プライバシー/データ関連、または曖昧である。推測せずユーザーに聞く。

迷ったら聞く。ノイズの多いコード品質コメントをスキップするのは安いが、本物のデータやセキュリティのバグをスキップするのは安くない。

## Learned pattern format

将来のパターンはこの形で追加する。

```markdown
### <short pattern name>

- Confidence: candidate | recurring | strong
- Skip when: <conditions that must be true>
- Do not skip when: <risk boundaries>
- Example signal: <phrases or code context that identify the pattern>
- Source: <PR/comment URL or short historical note>
```

1、2 個の例には `candidate` を使う。複数の実際の dismissal の後は `recurring` を使う。パターンが狭く、繰り返し検証されており、低リスクの場合にのみ `strong` を使う。

## Recurring skip candidates

### Intentional UI or design-system visual changes

- Confidence: candidate
- Skip when: PR の説明、スクリーンショット、design review、または近くのコードが視覚的な変更を明示的に示しており、review-bot のコメントは共有された視覚的デフォルトが変わったことを繰り返しているだけである。
- Do not skip when: コメントがアクセシビリティ、フォーカスの可視性、キーボードナビゲーション、色のコントラスト、または PR が意図的に変更していないコンポーネント API 契約を指している。
- Example signal: フォーカスアウトライン、ボタンサイズ、spacing、共有コンポーネントの視覚的デフォルトについてのコメントで、owner が「意図的」「意図通り」と返信している場合。

### Upstack or stack-local usage the review bot cannot see

- Confidence: candidate
- Skip when: review bot が export、コンポーネント、helper、ファイルを未使用としてフラグを立てているが、上位スタックの diff や PR の文脈から、スタック内の後の PR で使われていることが分かる。
- Do not skip when: 現在の PR がスタックの一部でない、その symbol が public API である、または想定される upstack での使用を検証できない。
- Example signal: 「エクスポートされたコンポーネントが使われていない」に対して、人間が「upstack で使われている」のように返信している。

### Temporary duplication during parallel implementation

- Confidence: candidate
- Skip when: PR が意図的に少量のコードを複製し、削除・置換・実証されようとしている旧 path と新しい path を並行させている。
- Do not skip when: 複製されたコードがセキュリティ、課金、データアクセス、API の挙動を変える、または長期的に共有される抽象化が明らかにリスクを下げる。
- Example signal: 「かなりの重複」「複製されたバリデーションロジック」に対して、owner が旧 path が削除される予定であること、あるいは複製ロジックが意図的にローカルであることを説明している。

### Existing framework or component invariant covers the warning

- Confidence: candidate
- Skip when: その懸念が、現在の diff や近くのコードに見える共有コンポーネント、framework の契約、型の不変条件、単一の信頼できる情報源によって、すでに保証されている。
- Do not skip when: その不変条件が想定されているだけで強制されていない、タイミングに依存している、または async/state の境界をまたいで値が乖離しうる。
- Example signal: 共有 popover が viewport の境界を強制しているのに inner popover に max-height がないというコメント、あるいはローカルでチェックされた値と渡された値が同じ source を共有している場合の nullable な値についてのコメント。

### Owner-declared follow-up or deferred cleanup

- Confidence: candidate
- Skip when: PR の owner がその問題は既知の follow-up であると明言しており、現在の PR で挙動が悪化しておらず、コメントが高リスクな領域についてのものでない。
- Do not skip when: agent が owner の input なしに動いている、その問題が medium/high severity のプロダクト挙動である、または deferring すると新しい regression がマージされてしまう。
- Example signal: 「それは後で気にする」「そのうち削除する」。

### Self-withdrawn or explicit false-positive rule comments

- Confidence: recurring
- Skip when: コメント本文、または後の review-bot の返信が、その finding が撤回された、準拠している、あるいは false positive であると明示的に述べており、agent が該当する rule をローカルで検証できる。
- Do not skip when: 唯一の証拠が、高リスクな issue に対して人間が説明なしに「false positive」と言っているだけである。
- Example signal: ファイル命名規則についてのコメントで、その本文がファイルはすでに準拠していると述べている。

## Ask by default

以前の PR で似たものが dismiss されていても、これらのカテゴリーは自動でスキップしない。

- セキュリティ、プライバシー、認証、課金、データ保持、training-data、権限境界に関する findings。
- severity の高い findings。
- migration、schema、冪等性、並行性、システム横断の挙動に関する findings。
- 提案された修正が小さく、プロダクトの意図を変えずに明らかにリスクを下げるコメント。

過去のデータでは、人間がセキュリティ/データフローのコメントを dismiss することがあると分かっている。それらはチーム全体のスキップルールではなく、owner の判断として扱う。

## Candidate learnings from recent PR-feedback passes

チームにとって有用そうだがまだ成熟していない候補の learning は、PR-feedback pass の最中または後にここへ追記する。いくつかの PR でパターンが確認されたら、recurring な候補を上のセクションに昇格させることを優先する。

### Manual reimplementations of native browser behavior

- Confidence: candidate
- Skip when: 実質的にほぼ never。diff がネイティブなブラウザ挙動を手動の同等物に置き換える場合（native sticky → JS で位置決めされた clone、native scroll targeting → 転送された wheel/touch イベント、paint-order occlusion → mask/clip-path）、そのコードに対する review bot のロジックバグの findings は一貫して正当だった。
- Do not skip when: そのコードにおける event-forwarding の隙間（wheel の deltaMode、touch のパン、端での scroll-chaining、tap slop）、mask/clip の hit-testing の乖離、observer と React の state のタイミングレースについての finding。デフォルトは fix。
- Example signal: 「masks do not affect hit-testing」「overlay blocks wheel scroll」「ignores deltaMode」「runs in the IntersectionObserver callback before React applies state」。
- Source: ある sticky-occlusion PR。review-bot のパス 6 回、約 18 個の findings、すべて dismiss ではなく fix された。

### Contract-test drift claims are cheaply verifiable — run the test first

- Confidence: candidate
- Skip when: 検証そのものは決してスキップしない。1 回のコマンドで済むからである。PR が protocol やドキュメントの文言を pin する contract test（SKILL.md に対する regex、doc の文言の snapshot）を出荷し、review bot が「テストがもうドキュメントと一致していない」（あるいはその逆）と主張する場合は、分類する前に PR の tip でそのテストを実行する。red な結果はその主張を経験的に確認し、green な結果は dismissal のための具体的な反証になる。
- Do not skip when: n/a。これは dismissal パターンではなく検証の近道である。繰り返しパスでの lean-dismiss ヒューリスティックはここで誤作動しうる点に注意する。prose-pinning テストは、まさに以前の修正ラウンドが文言を編集したせいで drift するからである。
- Example signal: 「Contract test omits the pre-fix wait」というコメントが、以前の修正 commit が pin された一節を書き換えた PR に対してなされ、tip でテストを実行すると、まさに指摘された assertion で失敗した。
- Source: review-bot のパス 8 回を経た、あるprose-pinning PR。その主張は、以前のすべてのパスが fix-and-resolved だったにもかかわらず、pass 7 では本物だった。

### Stale security-review finding already fixed later in the same PR

- Confidence: candidate
- Skip when: agentic security review（またはそれに類するもの）が、欠けている authz/validation の呼び出しを主張しており、現在の PR の tip がまさにその gate（テスト付き）を含んでいる。通常は review が走った後の後続の hardening commit で追加されたもの。
- Do not skip when: 指摘された helper が、議論の対象となる principal に対して no-op である、そのチェックが守るべき side effect の後に実行される、あるいは主張された principal のカバレッジが欠けている。
- Example signal: HIGH の「missing authorization check」の finding が出ているが、tip ではまさにその guard が side effect の前にすでに呼ばれている。
- Source: hardening commit が review の実行後にできた、あるwebhook-endpoint PR。

### Widening a deliberately narrow error condition would mask the real error

- Confidence: candidate
- Skip when: finding が、狭いエラー条件（特定の `errno`、エラーコード、status class）を catch-all に広げるよう求めており、その狭さが本物の区別をエンコードしている。典型的な形は `ENOENT` で gate された dependency fallback である。「binary がインストールされていない」は「コマンドは実行されたが失敗した」とは違う状況である。0 でない exit すべてで retry すると、正当な失敗（not found、期限切れの auth、network）を fallback に対して再実行し、fallback のエラーを報告してしまい、本当のエラーを隠してしまう。
- Do not skip when: 狭い条件が同じカテゴリー内の別のケース（別の「binary が使用不能」の errno、たとえば `EACCES`、別の transport レベルの失敗）を見落としている、処理されない path がデータを失うか部分的な状態を残す、あるいは retry が冪等であり、かつ元のエラーが依然として表面化している。
- Example signal: 「X が ENOENT で失敗したときだけ retry する……動作する Y が存在していても fallback を試さない」というコメントが、失敗したオペレーションではなく欠けている依存関係のために存在する fallback を持つコードを指している。
- Source: 欠けている binary のために存在する fallback を持つ、あるCLI-rename PR。
