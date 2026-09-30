#!/bin/sh
# 法规库「提取 / 判定分离」一键流程。
#
#   ./semantic_build.sh extract   提取所有批次（--dump-clauses 导出待判条文），
#                                 若已有语义判定 all-class.json 则一并回填重建，再跨批合并
#   ./semantic_build.sh chunk     把待判条文切成 ~165 条/块，交子代理判定
#   ./semantic_build.sh collect   把各块的判定结果 class-*.json 合并成 all-class.json
#
# 完整流程：extract → （我逐块做语义判定）→ collect → extract（再跑一次，回填）→ 完。
set -e
cd "$(dirname "$0")/.."                     # → ng-platform
PY=.venv/bin/python
REGS="$HOME/ng-regs"
CLS="$REGS/.classify"                        # 待判条文 / 判定结果都放这
BATCHES="$REGS/.batches"
mkdir -p "$CLS"

cmd="${1:-}"
case "$cmd" in
extract)
  # 有语义判定就回填（--classification），没有就只出关键词版 + 导出待判条文
  CLASSARG=""
  [ -f "$CLS/all-class.json" ] && CLASSARG="--classification $CLS/all-class.json"
  for L in "$BATCHES"/*.list; do
    b=$(basename "$L" .list)
    $PY scripts/reg_build.py --files-from "$L" --batch "$b" --out "$REGS" \
        --dump-clauses "$CLS/$b.jsonl" $CLASSARG 2>&1 | grep -E '条文|已导出' || true
  done
  SRC="/Volumes/LaCie/法规汇编/0 特定单位相关法规"
  if [ -d "$SRC" ]; then
    $PY scripts/reg_build.py --src "$SRC" --batch 特定单位相关法规 --out "$REGS" \
        --dump-clauses "$CLS/特定单位相关法规.jsonl" $CLASSARG 2>&1 | grep -E '条文|已导出' || true
  fi
  $PY scripts/reg_merge.py
  echo "✓ 提取+合并完成。待判条文在 $CLS/*.jsonl"
  ;;

chunk)
  $PY - <<'PYEOF'
import json, glob, os, shutil
CLS = os.path.expanduser("~/ng-regs/.classify")
os.makedirs(f"{CLS}/chunks", exist_ok=True)
rows = []
for f in sorted(glob.glob(f"{CLS}/*.jsonl")):
    for l in open(f, encoding="utf-8"):
        l = l.strip()
        if l:
            rows.append(json.loads(l))
# 已判过的不再切（all-class.json + 各块 class-*.json 都算）
done = set()
for f in [f"{CLS}/all-class.json"] + glob.glob(f"{CLS}/chunks/class-*.json") \
        + glob.glob(f"{CLS}/class-*.json"):
    if os.path.exists(f):
        done |= set(json.load(open(f, encoding="utf-8")).keys())
todo = [r for r in rows if r["id"] not in done]
for f in glob.glob(f"{CLS}/chunks/chunk-*.jsonl"):
    os.remove(f)
sz, n = 165, 0
for i in range(0, len(todo), sz):
    part = todo[i:i + sz]
    with open(f"{CLS}/chunks/chunk-{n}.jsonl", "w", encoding="utf-8") as fh:
        for r in part:
            fh.write(json.dumps({"id": r["id"], "doc": r["doc"], "unit": r["unit"],
                                 "text": r["text"], "kw_taxes": r["kw_taxes"],
                                 "kw_rules": r["kw_rules"]}, ensure_ascii=False) + "\n")
    n += 1
print(f"待判 {len(todo)} 条 → {n} 块（已判过 {len(done)} 条自动跳过）")
PYEOF
  ;;

collect)
  $PY - <<'PYEOF'
import json, glob, os
CLS = os.path.expanduser("~/ng-regs/.classify")
m = {}
for f in sorted(glob.glob(f"{CLS}/chunks/class-*.json")) + sorted(glob.glob(f"{CLS}/class-*.json")):
    m.update(json.load(open(f, encoding="utf-8")))
out = f"{CLS}/all-class.json"
# 已有 all-class.json 里、这次没重判的条要保留（块可能只覆盖新增部分）
if os.path.exists(out):
    old = json.load(open(out, encoding="utf-8"))
    old.update(m)
    m = old
if not m:
    print("✗ 没有找到任何 class-*.json，all-class.json 保持不变")
    raise SystemExit(1)
json.dump(m, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print(f"✓ 合并判定 {len(m)} 条 → {out}")
PYEOF
  ;;

*)
  echo "用法: $0 {extract|chunk|collect}"
  echo "  extract  提取所有批次(+回填已有判定)+跨批合并"
  echo "  chunk    切块交 LLM 判定"
  echo "  collect  合并各块判定结果"
  exit 1
  ;;
esac
