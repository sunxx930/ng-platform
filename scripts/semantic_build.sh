#!/bin/sh
# 法规库「提取 / 判定分离」一键流程。
#
#   ./semantic_build.sh extract [批次名...]  提取批次（导出待判条文）+ 回填已有判定 + 跨批合并。
#                                            **只跑"有新条文还没判定"或点名的批次**；
#                                            判完的老批次直接跳过，不重解析 PDF。
#   ./semantic_build.sh chunk                把待判条文切成 ~165 条/块，交子代理判定
#   ./semantic_build.sh collect              把各块判定结果 class-*.json 合并成 all-class.json
#   ./semantic_build.sh merge                 只重跑跨批合并（不重解析 PDF）
#
# 完整流程：extract 新批次 → chunk →（逐块做语义判定）→ collect → extract 新批次（回填）→ 归组。
set -e
export LC_ALL=en_US.UTF-8 2>/dev/null || true   # macOS sh 默认 locale 下中文回显会乱码
cd "$(dirname "$0")/.."                     # → ng-platform
PY=.venv/bin/python
REGS="$HOME/ng-regs"
CLS="$REGS/.classify"                        # 待判条文 / 判定结果都放这
BATCHES="$REGS/.batches"
mkdir -p "$CLS"
SRC_UNITS="/Volumes/LaCie/法规汇编/0 特定单位相关法规"

# 该批次是否需要重建：没建过 / 有未判定的 id / **判定内容变了**（哈希戳不符）→ 需要
_need() {
  $PY - "$1" <<'PY'
import hashlib, json, os, sys
b = sys.argv[1]; CLS = os.path.expanduser("~/ng-regs/.classify")
d = f"{CLS}/{b}.jsonl"
if not os.path.exists(d) or not os.path.isdir(os.path.expanduser(f"~/ng-regs/{b}")):
    raise SystemExit(0)                                   # 没导出过 / 没建过 → 需要
ids = [json.loads(l)["id"] for l in open(d, encoding="utf-8") if l.strip()]
a = f"{CLS}/all-class.json"
have = json.load(open(a, encoding="utf-8")) if os.path.exists(a) else {}
if any(i not in have for i in ids):
    raise SystemExit(0)                                   # 有未判条 → 需要
h = hashlib.sha1(json.dumps([(i, have[i]) for i in sorted(ids)],
                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
mark = f"{CLS}/.built/{b}.hash"
old = open(mark).read().strip() if os.path.exists(mark) else ""
raise SystemExit(0 if h != old else 1)                    # 判定变了 → 需要
PY
}

# 建完把该批次的判定哈希记下，供下次 _need 比对
_stamp() {
  $PY - "$1" <<'PY'
import hashlib, json, os, sys
b = sys.argv[1]; CLS = os.path.expanduser("~/ng-regs/.classify")
os.makedirs(f"{CLS}/.built", exist_ok=True)
d = f"{CLS}/{b}.jsonl"; a = f"{CLS}/all-class.json"
ids = [json.loads(l)["id"] for l in open(d, encoding="utf-8") if l.strip()]
have = json.load(open(a, encoding="utf-8")) if os.path.exists(a) else {}
h = hashlib.sha1(json.dumps([(i, have.get(i)) for i in sorted(ids)],
                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
open(f"{CLS}/.built/{b}.hash", "w").write(h)
PY
}

cmd="${1:-}"
case "$cmd" in
extract)
  shift 2>/dev/null || true
  FORCE=" $* "
  CLASSARG=""
  [ -f "$CLS/all-class.json" ] && CLASSARG="--classification $CLS/all-class.json"
  for L in "$BATCHES"/*.list; do
    b=$(basename "$L" .list)
    case "$FORCE" in *" $b "*) : ;; *) _need "$b" || { echo "  跳过 $b（无新条文）"; continue; } ;; esac
    $PY scripts/reg_build.py --files-from "$L" --batch "$b" --out "$REGS" \
        --dump-clauses "$CLS/$b.jsonl" $CLASSARG 2>&1 | grep -E '条文|已导出' || true
    _stamp "$b"
  done
  if [ -d "$SRC_UNITS" ]; then
    case "$FORCE" in *" 特定单位相关法规 "*) : ;; *) _need 特定单位相关法规 || n=skip ;; esac
    if [ "${n:-}" != skip ]; then
      $PY scripts/reg_build.py --src "$SRC_UNITS" --batch 特定单位相关法规 --out "$REGS" \
          --dump-clauses "$CLS/特定单位相关法规.jsonl" $CLASSARG 2>&1 | grep -E '条文|已导出' || true
      _stamp 特定单位相关法规
    else
      echo "  跳过 特定单位相关法规（无新条文）"
    fi
  fi
  $PY scripts/reg_merge.py
  echo "✓ 提取+合并完成。待判条文在 $CLS/*.jsonl（用 chunk 切块）"
  ;;

merge)
  $PY scripts/reg_merge.py
  ;;

regroup)
  # 新桶（没旧产物）→ 整份归组的批；变动桶 → 增量小任务（只插新增条文）
  $PY scripts/reg_semantic_prep.py --threshold 10 --cap 150
  $PY scripts/reg_semantic_diff.py
  ;;

regroup-apply)
  $PY scripts/reg_semantic_apply.py
  ;;

chunk)
  $PY - <<'PYEOF'
import json, glob, os
CLS = os.path.expanduser("~/ng-regs/.classify")
os.makedirs(f"{CLS}/chunks", exist_ok=True)
rows = []
for f in sorted(glob.glob(f"{CLS}/*.jsonl")):        # 只扫批次导出文件，不扫子目录
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
if os.path.exists(out):                     # 已有判定里、这次没重判的要保留
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
  echo "用法: $0 {extract [批次名...]|chunk|collect|merge|regroup|regroup-apply}"
  echo "  extract       提取「有新条文没判定」的批次(或点名的批次)+回填+跨批合并"
  echo "  chunk         切块交 LLM 判定"
  echo "  collect       合并各块判定结果"
  echo "  merge         只重跑跨批合并"
  echo "  regroup       出归组任务：新桶整份归组 / 变动桶增量任务(只插新条文)"
  echo "  regroup-apply 套用增量归组结果并校验"
  exit 1
  ;;
esac
