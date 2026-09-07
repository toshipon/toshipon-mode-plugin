### Doc author

**ドキュメントはあなたが持つものであり、台帳の view である。** 「要求定義書を書いて」「設計書に起こして」「ドキュメントを更新して」向け。`facts.md` と `questions.tsv` が既にあることが前提である。なければ Doc discovery から始める。台帳にない記述を成果物に書きたくなったら、それは Discovery か Question loop の仕事である。

台帳と規約は [`references/doc-workspace.md`](../references/doc-workspace.md) にある。

1. skeleton を選ぶ。要求定義は `references/skeletons/requirements.md`、設計書は `references/skeletons/design.md`。他の種類（運用手順、監査対応資料）が要る場合は、他の playbook が独自 playbook を設計するのと同じように skeleton を先に設計する。**How to read this**、札の規約、`## 未決事項`、`## Appendix A. 出典一覧` を必ず持たせ、`references/skeletons/` への追加候補として reply で提示する。Diátaxis のモードを skeleton の **How to read this** の通りに守る。要求定義は reference、設計書は explanation と reference。
2. 台帳が足りているかを確認する。skeleton の節を上から順に見て、`facts.md` に対応する事実があるかを問う。骨格を阻む `open` か `asked` の質問があれば Doc question loop に戻るか、推奨回答で仮置き（`assumed`）して進める。仮置きは `decisions.tsv` に残す。
3. 下書きする。skeleton をコピーし、見出しの順序を保ったまま埋める。すべての記述に札を付ける。要求は `- REQ-001. <要求> [S<n>]` の形、外部要件は表で `EXT-` と `REQ-` を対応付ける。**technical-writing** skill に完全に従い、その後 **unslop** をかける。long dash は禁止。文中のコロンも禁止。各見出しは記述する対象か発見を述べる。
4. 設計書なら決定を探索する。要となる設計判断ごとに `architect` agent（`subagent_type: "architect"`）を並列で走らせ、代替案とトレードオフを「決定と代替案」の表に落とす。各行は `[S]` か `[Q]` の根拠を持つ。スキップは `architect skipped: <reason>` としてリストに残す。設計判断を本文に紛れ込ませない。
5. 検証 gate を通す。3 つすべてが通って初めて検証済みである（[prove-it-works](../references/principles/prove-it-works.md)）。
   - **Traceability。** `scripts/doc-check.sh <workspace>` をエラー 0 にする。札のない要求、台帳にない `S`/`Q`、未決事項に載っていない `open` な `Q`、要求定義書と設計書の間の `REQ-` の対応、long dash と文中コロンを検査する。
   - **Adversarial review。** 成果物のパスを渡して `multi-role` か `interrogate` を走らせる。役割は 4 つ。監査人（「この統制の記述は監査で証跡として通るか」「条項の引用は版と一致するか」）、運用（「障害時と権限棚卸しの手順が書かれているか」）、セキュリティ、実装者（「この設計から実装に着手できるか、曖昧な節はどこか」）。指摘は 3 通りに捌く。直す、具体的な理由を付けて却下する、人が決めることなら `question.sh add` で質問にする。捌いた結果を `decisions.tsv` に残す。
   - **Skeleton 照合。** 書き終えた成果物を skeleton と並べ、見出しの有無と順序、**How to read this** の文言、`## 未決事項` と台帳の一致を目視で確認する（[encode-lessons-in-structure](../references/principles/encode-lessons-in-structure.md)）。3 つ目の成果物で 3 回目もこの照合を手でやっているなら、`doc-check.sh` に足す。
6. 行き先に合わせて仕上げる。git 管理なら `sources/` を含めずに PR を開く。commit の形は `${CLAUDE_PLUGIN_ROOT}/skills/commit-rules/SKILL.md` に従う。Confluence に転記するなら、表が pipe table であること、mermaid が転記先で描画されることを確認し、描画されなければ PNG か表に落とす。質問票は `scripts/question.sh <tsv> md` で owner ごとの表にして一緒に貼る。
7. 引き渡す。成果物のパス、review で覆した点、残った未決事項を `decisions.tsv` に 1 行残して止まる。

**Reply:** 成果物のパス、要求の件数と外部要件との対応、未決事項（owner 付き）、仮置きの一覧、adversarial review で直した点と却下した点、次に走らせる playbook。
