#!/usr/bin/env bash
# Traceability check for a toshipon-mode workspace. Usage: doc-check.sh <workspace-dir>
set -euo pipefail

usage() {
	printf 'usage: doc-check.sh <workspace-dir>\n' >&2
	exit 1
}

[ "$#" -eq 1 ] || usage
ws="$1"
[ -d "$ws" ] || usage

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/doc-check.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT

sources_tsv="$ws/sources.tsv"
questions_tsv="$ws/questions.tsv"

sources_ids="$tmpdir/sources.ids"
: > "$sources_ids"
if [ -f "$sources_tsv" ]; then
	awk -F'\t' 'NR>1 && $1!="" { print $1 }' "$sources_tsv" > "$sources_ids"
fi

questions_info="$tmpdir/questions.info"
: > "$questions_info"
if [ -f "$questions_tsv" ]; then
	awk -F'\t' 'NR>1 && $1!="" { print $1"\t"$5 }' "$questions_tsv" > "$questions_info"
fi

defs_out="$tmpdir/req_defs.tsv"
mentions_out="$tmpdir/req_mentions.tsv"
s_out="$tmpdir/s_refs.tsv"
q_out="$tmpdir/q_refs.tsv"
und_out="$tmpdir/undecided.tsv"
: > "$defs_out"
: > "$mentions_out"
: > "$s_out"
: > "$q_out"
: > "$und_out"

scan_awk="$tmpdir/scan.awk"
cat > "$scan_awk" <<'AWKEOF'
function has_tag(l) {
	if (l ~ /\[S[0-9]+\]/) return 1
	if (l ~ /\[Q[0-9]+( 仮)?\]/) return 1
	if (l ~ /\[D[0-9]+\]/) return 1
	return 0
}
BEGIN {
	in_fence = 0
	in_undecided = 0
}
{
	line = $0
	if (line ~ /^```/) {
		in_fence = !in_fence
		next
	}
	if (in_fence) next

	if (line ~ /^## /) {
		in_undecided = (line ~ /^## 未決事項/) ? 1 : 0
	}

	if (match(line, /^[ \t]*[-*]?[ \t]*REQ-[0-9]+\./)) {
		reqid = line
		if (match(line, /REQ-[0-9]+/)) {
			reqid = substr(line, RSTART, RLENGTH)
		}
		if (!has_tag(line)) {
			printf "ERROR %s:%d requirement definition missing an evidence tag: %s\n", file, NR, reqid
		}
		print reqid "\t" file "\t" NR >> defs_out
	}

	tmp = line
	while (match(tmp, /REQ-[0-9]+/)) {
		mid = substr(tmp, RSTART, RLENGTH)
		print mid "\t" file "\t" NR >> mentions_out
		tmp = substr(tmp, RSTART + RLENGTH)
	}

	tmp = line
	while (match(tmp, /\[S[0-9]+\]/)) {
		tag = substr(tmp, RSTART + 1, RLENGTH - 2)
		print tag "\t" file "\t" NR >> s_out
		tmp = substr(tmp, RSTART + RLENGTH)
	}

	tmp = line
	while (match(tmp, /\[Q[0-9]+( 仮)?\]/)) {
		full = substr(tmp, RSTART, RLENGTH)
		qid = full
		gsub(/[\[\]]/, "", qid)
		hasu = 0
		if (qid ~ / 仮$/) {
			hasu = 1
			gsub(/ 仮$/, "", qid)
		}
		print qid "\t" file "\t" NR "\t" hasu >> q_out
		tmp = substr(tmp, RSTART + RLENGTH)
	}

	if (in_undecided) {
		tmp = line
		while (match(tmp, /Q[0-9]+/)) {
			print substr(tmp, RSTART, RLENGTH) "\t" file >> und_out
			tmp = substr(tmp, RSTART + RLENGTH)
		}
	}

	trimmed = line
	gsub(/^[ \t]+/, "", trimmed)
	if (trimmed !~ /^\|/) {
		stripped = line
		gsub(/https?:\/\/[^ \t]+/, "", stripped)
		flagged = 0
		if (stripped ~ /—/) flagged = 1
		if (stripped ~ /–/) flagged = 1
		if (match(stripped, /：[^ \t]/)) flagged = 1
		if (match(stripped, /: [^ \t]/)) flagged = 1
		if (flagged) {
			printf "WARN %s:%d prose uses dash/colon-style punctuation; prefer plain Japanese sentence style\n", file, NR
		}
	}
}
AWKEOF

report="$tmpdir/report.txt"
: > "$report"

for f in "$ws"/*.md; do
	[ -e "$f" ] || continue
	base="$(basename "$f")"
	[ "$base" = "facts.md" ] && continue
	awk -v file="$base" -v defs_out="$defs_out" -v mentions_out="$mentions_out" \
		-v s_out="$s_out" -v q_out="$q_out" -v und_out="$und_out" -f "$scan_awk" "$f" >> "$report"
done

# check8: duplicate REQ-<n> definitions
awk -F'\t' '
	{
		if (seen[$1]++) {
			printf "ERROR %s:%d duplicate requirement definition: %s\n", $2, $3, $1
		}
	}
' "$defs_out" >> "$report"

# check2: [S<n>] refs must exist in sources.tsv
awk -F'\t' -v ids="$sources_ids" '
	BEGIN { while ((getline line < ids) > 0) valid[line] = 1 }
	{
		if (!($1 in valid)) {
			printf "ERROR %s:%d unknown source referenced: [%s]\n", $2, $3, $1
		}
	}
' "$s_out" >> "$report"

# check3 + check4: [Q<n>] refs must exist, and open/asked must be listed in that file's 未決事項
awk -F'\t' -v qinfo="$questions_info" -v und="$und_out" '
	BEGIN {
		while ((getline line < qinfo) > 0) {
			split(line, f, "\t")
			known[f[1]] = 1
			status[f[1]] = f[2]
		}
		while ((getline line < und) > 0) {
			split(line, f, "\t")
			listed[f[1], f[2]] = 1
		}
	}
	{
		qid = $1; qfile = $2; ln = $3; hasu = $4
		if (!(qid in known)) {
			printf "ERROR %s:%d unknown question referenced: [%s]\n", qfile, ln, qid
			next
		}
		st = status[qid]
		if (st == "open" || st == "asked") {
			if (!((qid, qfile) in listed)) {
				printf "ERROR %s:%d question %s (status: %s) is not listed under ## 未決事項 in %s\n", qfile, ln, qid, st, qfile
			}
		} else if (st == "assumed") {
			if (hasu != "1") {
				printf "WARN %s:%d question %s is assumed but referenced without the 仮 marker\n", qfile, ln, qid
			}
		} else if (st == "dropped") {
			printf "WARN %s:%d dropped question %s is still referenced\n", qfile, ln, qid
		}
	}
' "$q_out" >> "$report"

if [ -f "$ws/requirements.md" ] && [ -f "$ws/design.md" ]; then
	# check5: REQ-<n> mentioned in design.md but not defined in requirements.md
	awk -F'\t' -v defs="$defs_out" '
		BEGIN {
			while ((getline line < defs) > 0) {
				split(line, f, "\t")
				if (f[2] == "requirements.md") reqdef[f[1]] = 1
			}
		}
		$2 == "design.md" {
			if (!($1 in reqdef)) {
				printf "ERROR %s:%d requirement %s referenced in design.md but not defined in requirements.md\n", $2, $3, $1
			}
		}
	' "$mentions_out" >> "$report"

	# check6: REQ-<n> defined in requirements.md but never mentioned in design.md
	awk -F'\t' -v mentions="$mentions_out" '
		BEGIN {
			while ((getline line < mentions) > 0) {
				split(line, f, "\t")
				if (f[2] == "design.md") seen_design[f[1]] = 1
			}
		}
		$2 == "requirements.md" {
			if (!(seen[$1]++)) {
				if (!($1 in seen_design)) {
					printf "WARN %s:%d requirement %s is defined but never mentioned in design.md\n", $2, $3, $1
				}
			}
		}
	' "$defs_out" >> "$report"
fi

cat "$report"

errors="$(grep -c '^ERROR ' "$report" || true)"
warnings="$(grep -c '^WARN ' "$report" || true)"
printf 'doc-check: %d errors, %d warnings\n' "$errors" "$warnings"

[ "$errors" -eq 0 ]
