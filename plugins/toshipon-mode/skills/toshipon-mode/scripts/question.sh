#!/usr/bin/env bash
# Question ledger (TSV). Usage: question.sh <tsv> <add|set|list|md> ...
set -euo pipefail

usage() {
	printf 'usage: question.sh <tsv> add <question> <blocks> <owner>\n' >&2
	printf '       question.sh <tsv> set <id> <field> <value>\n' >&2
	printf '       question.sh <tsv> list [--status <s>] [--owner <o>]\n' >&2
	printf '       question.sh <tsv> md [--status <s>] [--owner <o>]\n' >&2
	exit 1
}

[ "$#" -ge 2 ] || usage

tsv="$1"
cmd="$2"
shift 2

tsvdir="$(dirname "$tsv")"
if [ -n "$tsvdir" ] && [ "$tsvdir" != "." ] && [ ! -d "$tsvdir" ]; then
	mkdir -p "$tsvdir"
fi

if [ ! -f "$tsv" ]; then
	printf 'id\tquestion\tblocks\towner\tstatus\tasked_at\tanswer\tevidence\tincorporated_in\tupdated\n' > "$tsv"
fi

# Same formula-injection guard as show-me-your-work/scripts/log.sh: strip
# tab/newline/CR so cells stay on one line, and quote-prefix any cell a
# spreadsheet would parse as a formula when this TSV is opened for review.
clean() {
	local v
	v=$(printf '%s' "$1" | tr '\t\n\r' '   ')
	case "$v" in
		=*|+*|-*|@*) printf "'%s" "$v" ;;
		*) printf '%s' "$v" ;;
	esac
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

next_id() {
	awk -F'\t' 'NR>1 && $1 ~ /^Q[0-9]+$/ { n = substr($1,2)+0; if (n>max) max=n } END { print max+0 }' "$tsv"
}

case "$cmd" in
	add)
		[ "$#" -eq 3 ] || usage
		n="$(next_id)"
		id="Q$((n+1))"
		ts="$(now)"
		printf '%s\t%s\t%s\t%s\topen\t\t\t\t\t%s\n' \
			"$id" "$(clean "$1")" "$(clean "$2")" "$(clean "$3")" "$ts" >> "$tsv"
		printf '%s\n' "$id"
		;;
	set)
		[ "$#" -eq 3 ] || usage
		id="$1"
		field="$2"
		value="$3"
		case "$field" in
			question|blocks|owner|status|asked_at|answer|evidence|incorporated_in) ;;
			*)
				printf 'error: unknown field: %s\n' "$field" >&2
				exit 1
				;;
		esac
		if ! awk -F'\t' -v id="$id" 'NR>1 && $1==id { found=1 } END { exit !found }' "$tsv"; then
			printf 'error: unknown id: %s\n' "$id" >&2
			exit 1
		fi
		ts="$(now)"
		cleanvalue="$(clean "$value")"
		tmpfile="$(mktemp "${tsv}.XXXXXX")"
		awk -F'\t' -v OFS='\t' -v id="$id" -v field="$field" -v value="$cleanvalue" -v ts="$ts" '
			NR==1 { print; next }
			$1==id {
				if (field=="question") $2=value
				else if (field=="blocks") $3=value
				else if (field=="owner") $4=value
				else if (field=="status") {
					$5=value
					if (value=="asked" && $6=="") $6=ts
				}
				else if (field=="asked_at") $6=value
				else if (field=="answer") $7=value
				else if (field=="evidence") $8=value
				else if (field=="incorporated_in") $9=value
				$10=ts
			}
			{ print }
		' "$tsv" > "$tmpfile"
		mv "$tmpfile" "$tsv"
		;;
	list)
		status_filter=""
		owner_filter=""
		while [ "$#" -gt 0 ]; do
			case "$1" in
				--status) status_filter="$2"; shift 2 ;;
				--owner) owner_filter="$2"; shift 2 ;;
				*) usage ;;
			esac
		done
		filtered="$(awk -F'\t' -v sf="$status_filter" -v of="$owner_filter" '
			NR==1 { print; next }
			(sf=="" || $5==sf) && (of=="" || $4==of) { print }
		' "$tsv")"
		if command -v column >/dev/null 2>&1; then
			printf '%s\n' "$filtered" | column -s$'\t' -t
		else
			printf '%s\n' "$filtered"
		fi
		;;
	md)
		status_filter=""
		owner_filter=""
		owner_given=0
		while [ "$#" -gt 0 ]; do
			case "$1" in
				--status) status_filter="$2"; shift 2 ;;
				--owner) owner_filter="$2"; owner_given=1; shift 2 ;;
				*) usage ;;
			esac
		done
		awk -F'\t' -v sf="$status_filter" -v of="$owner_filter" -v og="$owner_given" '
			function esc(s) { gsub(/\|/, "\\|", s); return s }
			function disp_owner(o) { return (o=="" ? "(未定)" : o) }
			function print_header() {
				print "| ID | 質問 | 阻んでいるもの | 回答者 | 状態 | 回答 |"
				print "| --- | --- | --- | --- | --- | --- |"
			}
			function print_row(id,q,b,o,st,ans) {
				print "| " esc(id) " | " esc(q) " | " esc(b) " | " esc(disp_owner(o)) " | " esc(st) " | " esc(ans) " |"
			}
			NR==1 { next }
			{
				if (sf!="" && $5!=sf) next
				if (og==1 && $4!=of) next
				row = $1 SUBSEP $2 SUBSEP $3 SUBSEP $4 SUBSEP $5 SUBSEP $7
				if (og==1) {
					n++
					rows[n] = row
					next
				}
				key = $4
				if (!(key in seen)) {
					seen[key] = 1
					order[++oc] = key
				}
				idx = gcount[key]++
				data[key, idx] = row
			}
			END {
				if (og==1) {
					print_header()
					for (i=1;i<=n;i++) {
						split(rows[i], f, SUBSEP)
						print_row(f[1],f[2],f[3],f[4],f[5],f[6])
					}
				} else {
					for (gi=1; gi<=oc; gi++) {
						key = order[gi]
						print "### " disp_owner(key)
						print ""
						print_header()
						cnt = gcount[key]
						for (i=0;i<cnt;i++) {
							split(data[key, i], f, SUBSEP)
							print_row(f[1],f[2],f[3],f[4],f[5],f[6])
						}
						print ""
					}
				}
			}
		' "$tsv"
		;;
	*)
		usage
		;;
esac
