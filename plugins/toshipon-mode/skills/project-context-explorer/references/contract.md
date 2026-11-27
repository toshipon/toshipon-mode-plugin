# Contract

`project-context-explorer` と `review-context-recovery` が共有するデータの形である。検査はすべて runtime（`runtime/context_recovery/`）が入力を受け取る時点で行う。ここに書いた形に合わない入力は、理由を並べて拒否される。

## Locator と revision

出典は locator と revision の組で指す。URL は使わない。

| locator | 指すもの | revision |
|---|---|---|
| `git:<path>` `git:<path>#L<a>` `git:<path>#L<a>-L<b>` | repository のファイル、またはその行 | commit sha（7〜40 桁の 16 進） |
| `pr:<番号>#comment-<ID>` `pr:<番号>#description` | PR のレビューコメント、本文 | head の commit sha |
| `doc:<source>:<id>` | 文書ツールの中の文書。`source` は `confluence`、`gdrive` など小文字の名前、`id` はそのツールの ID | 版の名前（`v3`、更新日時など。80 文字まで） |

git の出典は、照合のたびに repository から読む（`git show <revision>:<path>`）。指せるのはファイルで、directory は出典にならない。path には制御文字、双方向制御文字、ゼロ幅文字を含められない。それ以外は、セッションが取得した本文を `evidence add` で snapshot に保存し、そこから読む。snapshot は repository の外に置かれ、秘密情報の形をした文字列は保存前に伏せられる。保存した snapshot は、同じ locator と revision では別の内容に替えられない。資料が変わったら、新しい版として保存する。

## Claim

```json
{
  "id": "C1",
  "status": "confirmed",
  "text": "PR は visibility timeout を 30 秒にする",
  "references": [
    {"locator": "git:infra/queue.tf#L4", "revision": "<sha>", "excerpt": "visibility_timeout_seconds = 30"}
  ],
  "uncertainty": "",
  "searched": []
}
```

| status | 意味 | 必要なもの |
|---|---|---|
| `confirmed` | 引いた出典にそう書いてある | locator、revision、引用が 1 つ以上 |
| `inferred` | 出典からの読み取りで、どこにも書かれていない | locator、revision、`uncertainty`（何が不確かか） |
| `conflict` | 出典どうしが食い違う | 食い違うそれぞれの出典からの引用（2 つ以上）、`uncertainty` |
| `unknown` | 探したが見つからなかった | `searched`（どこを探したか） |

引用は 6〜600 文字である。照合（`claims check`）は、引用が出典のその revision に、空白の違いを除いてそのまま現れるかを見る。行の指定があれば、その行の範囲だけを見る。出典が読めない、行が範囲の外、引用が無い、のどれかがあると、その主張は unsupported になる。unsupported claim rate は、unknown を除いた主張のうち unsupported の割合である。

照合が確かめるのは引用の実在で、主張の文が引用から正しく導かれているかは確かめない。inferred の推論が妥当かどうかも確かめない。

### ほかの語彙との対応

| ここでの status | `why` skill の確信度 | sre-harness の `basis` |
|---|---|---|
| confirmed | Direct | explicit |
| inferred | Supported / Inferred / Speculative（強さは `uncertainty` に書く） | inferred |
| conflict | Competing Hypotheses に置くもの | — |
| unknown | Unknown | — |

言い回しは `why` skill の `references/epistemics.md` に従う。

## Entity

```json
{
  "id": "PROP-retention",
  "type": "property",
  "description": "orders queue の保持期間（message_retention_days）",
  "config_path": "infra/queue.tf",
  "source": {"locator": "git:infra/queue.tf#L3"},
  "relationships": [{"type": "decided_by", "to": "DEC-0001"}],
  "status": "approved",
  "rationale_recorded": true
}
```

- `id`: 英字で始まる 80 文字までの名前（英数字と `_ . : -`）。
- `type`: `objective` `component` `resource` `property` `requirement` `decision` `change` `meeting_note` `document` `risk` `question`。
- `description`: 1 行、240 文字まで。本文は入れない。
- `source`: locator だけを書けば、git の出典には `index add` が HEAD の revision・sha256（該当行の hash）・size を記録する。revision を書いた場合は、そのまま記録する。
- `relationships[].type`: `refines` `implements` `verifies` `depends_on` `conflicts_with`（sre-harness の trace と同じ名前）、`part_of` `supersedes` `decided_by` `discussed_in` `changed_by`。
- `status`: 決定の状態。sre-harness の Decision Registry と同じ `proposed` `approved` `rejected` `superseded` `unresolved`。
- `rationale_recorded`: 理由が出典に書かれているか。registry から取り込んだ決定と要求には自動で付く。付いていない `resource` と `property` は、`decided_by` / `discussed_in` / `implements` / `refines` の関係が 1 つも無ければ `missing_rationale` になる。

決定と要求の台帳そのものは sre-harness の Specification Consistency が持つ。この索引は、それを読んで entity にするだけで、承認や置換の規則は持たない。

## Check の結果

| kind | 意味 |
|---|---|
| `stale_reference` | 索引に記録した hash と、今の HEAD での該当行の hash が違う |
| `missing_source` | 出典のファイルか行が HEAD に無い |
| `missing_rationale` | 理由を書いた出典が記録されていない |
| `conflict` | `conflicts_with` の関係がある。両方が approved なら、その旨が付く |
| `dangling_reference` | 関係の相手が索引に無い |
| `no_source` | 出典の locator が無い |
| `source_unavailable` | その分類の資料に、このセッションから届かない |

git の出典は、hash があれば hash と、revision だけなら当時の該当行と、今の HEAD の該当行を比べる。git 以外の出典（PR、文書）が古くなったかどうかは、取得し直さないと分からないので検査しない。記録された revision が repository に無い出典も比べられない。これらの件数は `not_checked` に出る。

## Exploration と context packet

`explore` は、seed（指定した entity、または問いに合う上位 3 件）から関係を両方向にたどる。上限は hop（既定 1、最大 4）、件数（既定 12、最大 40）、token の予算（既定 4000、最大 60000）で、返すのは metadata と取得手順だけである。入らなかったものは `frontier`、`dropped`、`deferred` に数えられる。token は UTF-8 の 3 byte を 1 token とする高めの見積もりである。

packet は次を持つ。

| 項目 | 内容 |
|---|---|
| `selected_entity`、`question`、`mode` | 対象、問い、mode |
| `references` | 範囲に入った entity の metadata（locator、revision、hash、距離） |
| `retrieval_instructions` | 出典ごとの取得手順。git は `git show`、PR と文書は「読み取り専用で取得する」という指示で、ツール名は書かない。英数字と `_ . / @ + = , -` 以外を含む path は手順に書かず、参照の表を見るよう指示する |
| `token_budget` | 予算、取得の見積もり、prompt 自体の見積もり |
| `claims` | 照合に通った主張 |
| `gaps` | 範囲に関係する check の結果 |
| `prompt` | 上を 1 つの文章にしたもの。新しいセッションに貼る |

## 取得した本文の扱い

資料から来た文字列（コメント、diff、entity の説明、問い、欠落の一覧、セッションの文脈）は、prompt の中では次の枠に入る。

```
<<<UNTRUSTED-DATA 0eb06743bf193c33 source=index instruction_like=yes>>>
...
<<<END-UNTRUSTED-DATA 0eb06743bf193c33>>>
```

境界の文字列は本文から計算し、本文に同じ文字列が現れないことを確かめてから使う。本文に閉じの行を書いても、枠は閉じない。制御文字と、Unicode の書式文字（双方向制御、ゼロ幅、soft hyphen など）、行・段落の区切り文字は取り除く。指示の形をした文（「これまでの指示を無視」など）があれば `instruction_like=yes` を付ける。これは読む側への目印で、検出できない書き方がある。防御の本体は、資料の文章を枠の外に置かないことである。枠の外に出るのは、script が持つ定型文と、形を検査した id・revision・PR 番号・文書の id、それに英数字と `_ . / @ + = , -` だけでできた path である。それ以外の path（空白や日本語を含むもの）は枠の中にだけ現れる。
