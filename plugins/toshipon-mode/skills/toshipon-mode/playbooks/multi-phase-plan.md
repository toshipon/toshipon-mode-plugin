### Multi-phase or multi-PR plan

**プランはあなたが持つものであり、コードではない。プランは owner が箱ごとに実行し、operator が証拠から監査するチェックリストである。** phase や積み重ねた PR にまたがる作業向け。プランそのものが成果物である。実装しない。

1. 変更が 1、2 ファイルでアプローチが明白な場合は、プランをスキップする。そう述べて止まる。
2. 書く前に、`playbooks/prototype.md` に従って未解決の問いを prototype で片付ける。operator に聞くのは、実行では決着しないプロダクトや好みの判断だけにする（[never-block-on-the-human](../references/principles/never-block-on-the-human.md)）。
3. プランファイルを書く。operator がパスを指定しない限り、リポジトリの `docs/` 配下に置く。冒頭に、何が変わるか、着地順の PR id、依存グラフを書く。依存グラフには、どの PR が独立していて trunk から分岐するか、どの PR がどの PR を待つか、各 PR が触ってよいファイルを書く。続けて 1 PR につき 1 セクションを書く。1 つの PR は自身の証拠を持つ 1 つの変更である（[sequence-verifiable-units](../references/principles/sequence-verifiable-units.md)）。各セクションには、依存先、編集するファイル、作る 1 つの変更、目に見える結果、検証のチェックボックスを置く。**technical-writing** skill に従って書き、**unslop** をかける。
4. テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みになる。live の箱は、driver skill（Non-negotiables 参照）で本物の surface を駆動し、具体的なシナリオと合格の述語を持つ。perf の箱は、指標、probe、先に測った trunk の baseline、失敗とする数値を名指しする。インタラクションを変える PR は、merge の前に operator のレビューを待つ。
5. プランのパスを渡して止まる。実行は operator の明示的な go を待って始める。

**Reply:** プランのパス、依存関係と review-gated set を伴う PR id、未証明のまま残ったこと。
