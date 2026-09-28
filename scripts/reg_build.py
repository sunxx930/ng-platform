#!/usr/bin/env python3
"""法规素材 → 条文级知识库（打破文件界限，按分类合并输出）。

素材有两种形态，脚本都要吃下：
  ① EY 税法数据库导出件：Part A(结构化元数据) + Part B(正文) + Part C(录入信息)
  ② 原生公文 PDF：直接就是正文，元数据只能从文件名/正文推

产物三层（同一条文在多层出现，且永远自带【文号 条号】）：
  按税种规则/<税种>/<处理规则>.md   —— 税种 × 该税种下的具体处理规则
  按事件/<主体>.md                  —— 同一事件的**跨税种条文组合**
  _索引.md                          —— 总览与待归类

用法:
  python3 scripts/reg_build.py --src "/Volumes/LaCie/法规汇编/0 特定单位相关法规" \
      --out ~/ng-regs
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import re
from pathlib import Path

import pymupdf

# ---------- 税种 / 处理规则 ----------
TAXES = ["增值税", "营业税", "印花税", "契税", "土地增值税", "城镇土地使用税", "房产税",
         "企业所得税", "个人所得税", "消费税", "资源税", "车船税", "城市维护建设税",
         "耕地占用税", "烟叶税", "船舶吨税", "关税", "教育费附加"]

# 旧称/别称 → 标准税种名（老文件里常写旧名，不映射会整份漏掉）
TAX_ALIAS = {
    "土地使用税": "城镇土地使用税",
    "车船使用税": "车船税",
    "车船使用牌照税": "车船税",
    "城市房地产税": "房产税",
    "工商统一税": "增值税",
    "产品税": "增值税",
    "进口环节增值税": "增值税",
    "进口税收": "关税",
}

# 处理规则 → 命中关键词。**一条条文允许多个归类**（用户 2026-09-28）：
# 例如「不征收过户环节的证券交易印花税」既属产权转移、也属免征项目。
# 顺序只用于展示排序，不再"先命中先用"。
RULES = [
    # —— 结构性条款（不是边角料：它们界定适用范围、解释概念、规定程序）——
    ("适用对象", ["各省、自治区、直辖市", "各计划单列市", "现将有关", "通知如下",
                  "批复如下", "公告如下", "收悉"]),
    ("定义", ["是指", "所称", "本办法所称", "本公告所称", "本通知所称", "系指"]),
    ("程序", ["报送", "备案", "申报", "出具", "确认表", "管理办法", "监督管理",
              "缴纳期限", "纳税期限", "划转入库", "征收管理"]),
    # —— 实体规则 ——
    ("源泉扣缴", ["代扣代缴", "源泉扣缴", "扣缴义务人"]),
    ("免征项目", ["免征", "免税", "免征项目", "免缴", "不征收", "不予征收"]),
    ("产权转移", ["产权转移", "股权转让", "过户", "书据"]),
    ("资金账簿", ["资金账簿", "账簿"]),
    ("购销合同", ["购销合同"]),
    # 「建筑安装」单独出现时多指企业类型（建筑安装企业），不是合同类型 —— 必须带"合同"才认
    ("工程合同", ["工程合同", "建筑安装合同", "建设工程合同", "安装工程合同"]),
    ("货运合同", ["货运合同", "运输合同"]),
    ("借款合同", ["借款合同"]),
    ("保险合同", ["保险合同"]),
    ("租赁合同", ["租赁合同", "经营租赁", "融资租赁"]),
    ("收入", ["收入", "所得额", "应税所得"]),
    ("成本费用的扣除", ["成本", "费用", "扣除"]),
    ("税收优惠", ["优惠", "减半", "减征", "先征后退", "即征即退", "退税"]),
]

# 主体：单位 / 辖区。特征强，**文件名、标题、正文都可认**
# （发文机关常只在正文里出现，如文件名只有「关于…的通告」而正文写着「国家税务总局深圳市税务局」）
SUBJECTS = [
    ("金融资产管理公司", ["资产公司", "资产管理公司", "信达", "华融", "长城资产", "东方资产"]),
    ("军队军工", ["军队", "军品", "军工", "军事", "保障性企业"]),
    ("核工业", ["核工业", "核电"]),
    ("社会保障基金", ["社会保障基金", "养老保险基金"]),
    ("证券投资者保护基金", ["证券投资者保护基金"]),
    ("保险保障基金", ["保险保障基金"]),
    ("消防救援队伍", ["消防救援"]),
    ("铁路", ["铁路", "铁道", "青藏铁路"]),
    ("邮政储蓄与三农", ["邮政储蓄", "三农", "涉农贷款"]),
    ("商品储备", ["商品储备", "粮棉", "粮油", "物资储备"]),
    ("银行业不良债权", ["以物抵债", "不良债权"]),
    ("农村信用社", ["农村信用社"]),
    ("西气东输", ["西气东输"]),
    ("兵器工业", ["兵器工业", "兵器装备"]),
    # 「公安」「司法」单独用会误伤（正文常出现「人民法院」等）——必须带"部门"
    ("公安司法", ["公安部门", "司法部门", "公安、司法", "公安部和司法部"]),
]

# 事项主体（强特征）：**可以认条文正文** —— 一条讲股权代持的条文，这份文件就是在讲它。
# 与「单位/辖区」的区别：单位类绝不能用正文判（实测：深圳一份房产税 Q&A 里有一条讲
# 「军队车船…免征车船使用税」，正文认「军队」就把整份错认成军队军工文件）。
TOPIC_SUBJECTS = [
    ("股权代持", ["股权代持", "代持关系", "显名股东", "隐名股东"]),
    ("股权变更登记", ["股权变更登记", "股东变更登记"]),
]

# 辖区主体：**只认文件名/标题，且优先用文件所在目录**。
# ⚠ 绝不能让辖区从正文认：全国性文件的发文对象里就列着「各省…北京市税务局…深圳市税务局…」，
#   一认正文就误判（实测：全国通用文件凭空冒出「北京市」事件 48 条；批次0 的铁路文件
#   也因为收文单位列表被认成北京市）。辖区可靠信号是**用户的目录划分**。
LOCALITY_DIRS = {
    "深圳": "深圳市", "上海": "上海市", "北京": "北京市", "厦门": "厦门市",
    "苏州": "苏州市", "珠海": "珠海市", "广东": "广东省", "横琴": "横琴",
}


def locality_hint(p: Path, title: str = "") -> str:
    """判辖区。**文件名优先，目录次之**（目录取最深的那个）。

    为什么文件名优先：省市目录是混排的 —— 实测「…/深圳地方法规/房产税…/1 广东省城镇土地
    使用税实施细则.pdf」放在深圳目录下，但它是**广东省**的文件；只看目录会错标成深圳市。
    目录取最深：越靠近文件的目录越具体。
    """
    for text in (title, p.name):
        for k, v in LOCALITY_DIRS.items():
            if k in text:
                return v
    for part in reversed(p.parts):          # 从最深的目录往上找
        for k, v in LOCALITY_DIRS.items():
            if k in part:
                return v
    return ""


# 事项维度（弱特征）：**只认标题**。
# 「地方教育附加」这类词通用优惠公告正文里常顺带提到（如六税一费公告），
# 拿正文判会把文件错认成"关于地方教育附加"（实测冒出过假事件）。
# 但标题写了《…地方教育附加征收管理办法》的，就确实是讲它 —— 所以只匹配标题。
TITLE_TOPICS = [
    ("地方教育附加", ["地方教育附加"]),
    ("教育费附加", ["教育费附加"]),
    ("增值税起征点", ["增值税起征点", "起征点"]),
]


def title_topic(title: str) -> str:
    """从标题里取出「事项」——仅在**主体识别不出**时用来给通用政策文件当事件名。

    如「关于延续实施养老、托育、家政等社区家庭服务业税费优惠政策的公告」
    → 「养老、托育、家政等社区家庭服务业税费优惠」。
    """
    t = re.sub(r"^关于(继续|延续)?(实施|执行)?", "", title or "")
    t = re.sub(r"(有关)?(税收|税费)?(政策)?(问题)?的(公告|通知|批复|函|规定|办法|通告)$", "", t)
    t = re.sub(r"[，,。；;]$", "", t).strip()
    return t if 3 <= len(t) <= 40 else ""

def short_title(title: str) -> str:
    """短标题：给文号消歧用（仅当同一文号被多份文件共用时）。

    如「关于个人所得税征收管理若干问题的公告（2018年修正）」→「个人所得税征收管理」。
    """
    t = re.sub(r"[（(][^）)]*(修正|修订|废止|修改)[^）)]*[）)]", "", title or "")
    t = re.sub(r"^(关于|印发|发布)", "", t)
    t = re.sub(r"(有关)?(税收|税费)?(政策)?(问题)?的(公告|通知|批复|函|规定|办法|通告|实施细则)$", "", t)
    t = re.sub(r"^(《|》)|(《|》)$", "", t).strip("《》 ")
    return (t[:16] or (title or "")[:12])


def _topic_covered(topic: str, subs: list[str]) -> bool:
    """标题事项是否已被某个主体覆盖（含包含关系，忽略标点）。"""
    def key(x: str) -> str:
        return re.sub(r"[^\w]", "", x or "")
    t = key(topic)
    if not t:
        return True
    return any(s and (key(s) in t or t in key(s)) for s in subs)


_CLAUSE = re.compile(r"^([一二三四五六七八九十百]+、|\d+[．.]|[（(][一二三四五六七八九十]+[)）]|第[一二三四五六七八九十百零\d]+条)")
_SENT_END = re.compile(r"[。；：！？:；]$")
_PAGENO = re.compile(r"^\d+$")
_JUNK = ("Tax Law", "Information in this database", "Due to time constraint",
         "Part A", "Part B", "Part C", "Proprietary and confidential")

_LABELS: list[str] = []      # EY 的 Part A 元数据已弃用（不是权威口径，且多数文件没有）
_LBL: set[str] = set()


def norm(s: str) -> str:
    """去标点空白，用于作废比对（批注覆盖文字与正文的标点常不一致）。"""
    return re.sub(r"[\s．.、，,。；;：:（）()【】\[\]《》]", "", s)


# 康熙部首 / CJK 部首码位 → 正体汉字（NFKC **归不掉**这些，必须显式映射）
# 来源：PDF 从税务网站复制时把汉字替换成了同形部首码位，如 ⼈(U+2F08) 代替 人。
# 不修的话「纳税人」搜不到正文里的「纳税⼈」。
_RADICALS = {
    "⺠": "民", "⻛": "风", "⻚": "页", "⻔": "门", "⻄": "西", "⻅": "见", "⻓": "长",
    "⻝": "食", "⻢": "马", "⻉": "贝", "⻋": "车", "⻘": "青", "⻜": "飞", "⻩": "黄",
    "⻰": "龙", "⻳": "龟", "⺟": "母", "⺘": "手", "⺙": "攴", "⺮": "竹", "⺯": "糸",
    "⺰": "糸", "⺱": "网", "⺲": "网", "⺳": "网", "⺴": "网", "⺵": "网", "⺶": "羊",
    "⺷": "羊", "⺸": "羊", "⺹": "老", "⺺": "而", "⺻": "耒", "⺼": "肉", "⺽": "肉",
    "⺾": "艸", "⺿": "艸", "⻀": "艸", "⻁": "虎", "⻂": "衣", "⻃": "西", "⻆": "角",
    "⻈": "言", "⻊": "足", "⻌": "辵", "⻍": "辵", "⻎": "辵", "⻏": "邑", "⻐": "酉",
    "⻑": "長", "⻒": "長", "⻕": "阜", "⻖": "阜", "⻗": "雨", "⻙": "韋", "⻞": "食",
    "⻟": "食", "⻠": "食", "⻣": "骨", "⻤": "鬼", "⻥": "魚", "⻦": "鳥", "⻧": "鹵",
    "⻨": "麥", "⻪": "黽", "⻫": "齊", "⻬": "齊", "⻭": "齒", "⻮": "齒", "⻯": "龍",
    "⻱": "龜", "⻲": "龜", "⽔": "水", "⽕": "火", "⽊": "木", "⽟": "玉", "⽣": "生",
    "⽬": "目", "⽭": "矛", "⽮": "矢", "⽯": "石", "⽰": "示", "⽲": "禾", "⽳": "穴",
    "⽴": "立", "⽵": "竹", "⽶": "米", "⽷": "糸", "⽸": "缶", "⽹": "网", "⽺": "羊",
    "⽻": "羽", "⽼": "老", "⽽": "而", "⽿": "耳", "⾁": "肉", "⾂": "臣", "⾃": "自",
    "⾄": "至", "⾅": "臼", "⾆": "舌", "⾇": "舛", "⾈": "舟", "⾉": "艮", "⾊": "色",
    "⾋": "艸", "⾌": "虍", "⾍": "虫", "⾎": "血", "⾏": "行", "⾐": "衣", "⾒": "見",
    "⾓": "角", "⾔": "言", "⾕": "谷", "⾖": "豆", "⾗": "豕", "⾘": "豸", "⾙": "貝",
    "⾚": "赤", "⾛": "走", "⾜": "足", "⾝": "身", "⾞": "車", "⾟": "辛", "⾠": "辰",
    "⾡": "辵", "⾢": "邑", "⾣": "酉", "⾤": "釆", "⾥": "里", "⾦": "金", "⾧": "長",
    "⾨": "門", "⾩": "阜", "⾪": "隶", "⾫": "隹", "⾬": "雨", "⾭": "青", "⾮": "非",
    "⾯": "面", "⾰": "革", "⾱": "韋", "⾲": "韭", "⾳": "音", "⾴": "頁", "⾵": "風",
    "⾶": "飛", "⾷": "食", "⾸": "首", "⾹": "香", "⾺": "馬", "⾻": "骨", "⾼": "高",
    "⾽": "髟", "⾾": "鬥", "⾿": "鬯", "⿀": "鬲", "⿁": "鬼", "⿂": "魚", "⿃": "鳥",
    "⿄": "鹵", "⿅": "鹿", "⿆": "麥", "⿇": "麻", "⿈": "黃", "⿉": "黍", "⿊": "黑",
    "⿋": "黹", "⿌": "黽", "⿍": "鼎", "⿎": "鼓", "⿏": "鼠", "⿐": "鼻", "⿑": "齊",
    "⿒": "齒", "⿓": "龍", "⿔": "龜", "⿕": "龠",
    "⼀": "一", "⼁": "丨", "⼂": "丶", "⼃": "丿", "⼄": "乙", "⼅": "亅", "⼆": "二",
    "⼇": "亠", "⼈": "人", "⼉": "儿", "⼊": "入", "⼋": "八", "⼌": "冂", "⼍": "冖",
    "⼎": "冫", "⼏": "几", "⼐": "凵", "⼑": "刀", "⼒": "力", "⼓": "勹", "⼔": "匕",
    "⼕": "匚", "⼖": "匸", "⼗": "十", "⼘": "卜", "⼙": "卩", "⼚": "厂", "⼛": "厶",
    "⼜": "又", "⼝": "口", "⼞": "囗", "⼟": "土", "⼠": "士", "⼡": "夂", "⼢": "夊",
    "⼣": "夕", "⼤": "大", "⼥": "女", "⼦": "子", "⼧": "宀", "⼨": "寸", "⼩": "小",
    "⼪": "尢", "⼫": "尸", "⼬": "屮", "⼭": "山", "⼮": "巛", "⼯": "工", "⼰": "己",
    "⼱": "巾", "⼲": "干", "⼳": "幺", "⼴": "广", "⼵": "廴", "⼶": "廾", "⼷": "弋",
    "⼸": "弓", "⼹": "彐", "⼺": "彡", "⼻": "彳", "⼼": "心", "⼽": "戈", "⼾": "戶",
    "⼿": "手", "⽀": "支", "⽁": "攴", "⽂": "文", "⽃": "斗", "⽄": "斤", "⽅": "方",
    "⽆": "无", "⽇": "日", "⽈": "曰", "⽉": "月", "⽊": "木", "⽋": "欠", "⽌": "止",
    "⽍": "歹", "⽎": "殳", "⽏": "毋", "⽐": "比", "⽑": "毛", "⽒": "氏", "⽓": "气",
}


# 「法律数据库导出件」形态（第三种）：开头是「标签：\n值」的元数据块
_META_LABELS = ("发文机关", "发布日期", "生效日期", "失效日期", "时效性", "文号",
                "相关法规", "法规文号", "效力状态")


def split_meta_head(raw: str) -> tuple[dict, str]:
    """剥离法律数据库导出件的元数据头，返回 (元数据, 正文)。

    形态：`<标题>\\n发文机关：\\n值\\n发布日期：\\n值…`，随后正文里**标题与文号又各出现一次**。
    元数据是公开发布信息（机关/日期/时效性），保留作文件级事实；
    但**不参与分类**——分类一律从条文正文自析。
    """
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    start = None
    for i, s in enumerate(lines[:12]):
        if s.rstrip("：:").strip() in _META_LABELS:
            start = i
            break
    if start is None:
        return {}, raw
    meta, i = {}, start
    while i < len(lines):
        key = lines[i].rstrip("：:").strip()
        if key in _META_LABELS and i + 1 < len(lines):
            meta[key] = lines[i + 1]
            i += 2
        else:
            break
    return meta, "\n".join(lines[i:])


def canonic(text: str) -> str:
    """NFKC 归一化 + 清洗网页复制噪声。

    原生公文多是从税务网站复制来的，会带：
      - 兼容/异体字符（⾼⽂⽇ 之类，NFKC 可还原成正体）
      - 站点头：搜索 / 高级搜索 / 成文日期 / 字体：… / 分享到 / 【打印】【下载】
    """
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans(_RADICALS))          # ← 部首码位还原成正体汉字
    drop = re.compile(r"^(搜索|高级搜索|成文日期|字体|分享到|【?打印】?$|【?下载】?$|"
                      r"发布日期|索引号|来源|字号|正文下载|相关文章|扫一扫|"
                      r"page\s*\d+\s*of\s*\d+|第\s*\d+\s*页\s*共\s*\d+\s*页)")

    def keep(l: str) -> bool:
        s = l.strip()
        # ⚠ 以冒号结尾的是**元数据标签行**（发文机关：/发布日期：/文号：…），
        # 不是网页噪声。NFKC 会把全角「：」变半角「:」。此前误删了标签行，
        # 导致整个元数据块解析中断、标签值被当条文。
        if s.endswith((":", "：")):
            return True
        return not drop.match(s)

    return "\n".join(l for l in text.splitlines() if keep(l))


def strip_web_prefix(body: str) -> str:
    """站点头常与首行正文粘成一行（…网站纠错财政部 税务总局关于…）→ 从标题处截断。

    ⚠ 只能在**正文**上做：带 re.S 的正则若跑在全文上，会把 EY 文档的 Part A 元数据一起吃掉。
    """
    return re.sub(r"^.*?(?=(?:财政部|国家税务总局|国务院|海关总署)[^\n]{0,20}(?:关于|公告|通知|令))",
                  "", body, count=1, flags=re.S)


_ANNEX = re.compile(r"^\s*(附件\s*\d|附\s*件\s*$|附表|免税目录|进口目录)")
_CIRC = re.compile(r"^(.+?(?:\[[^\]]*\]第?\d+号|(?:公告|通告|令)\s*\d{4}\s*年第?\s*\d+\s*号))")


def split_annex(body: str) -> tuple[str, str]:
    """正文 / 附件分开。

    附件常是大表格（如《消防救援装备进口免税目录》：序号/装备名称/税则号列/性能指标），
    直接按行切会把每一格都当条文 —— 必须分出。**不丢弃**，单独留存。
    """
    lines = body.splitlines()
    for i, l in enumerate(lines):
        if _ANNEX.match(l):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return body, ""


def strip_header_lines(body: str, title: str, circ: str) -> str:
    """去掉正文开头的公文头（标题行 + 文号行）——**在合并之前做**。

    原生公文的开头是：
        财政部 税务总局关于继续实施部分国家商品储备税收优惠政策的公告
        财政部 税务总局公告2023年第48号
        为继续支持国家商品储备,现将…
    前两行是抬头、不是条文；标题与文号已进文件元数据。若不在合并前去掉，
    会被并进「前言」，出现标题重复、文号粘连（用户 2026-09-28 指出要只留一个）。
    """
    lines = body.splitlines()
    out = []
    title_done = circ_done = False
    # 只比「年份+编号」：文件名写「国家税务总局」而正文常写「税务总局」，部委名比对会漏
    m = re.search(r"(\d{4})[^\d]{0,4}(\d+)\s*号", circ)
    year, num = (m.group(1), m.group(2)) if m else ("", "")

    def _is_circ_line(s: str) -> bool:
        return bool(year and re.fullmatch(r"[^\n]{0,30}号", s) and year in s
                    and re.search(rf"{re.escape(year)}[^\d]{{0,4}}{re.escape(num)}号", s))

    def _cut_circ(s: str) -> str:
        if not year:
            return s
        m2 = re.search(rf"{re.escape(year)}[^\d]{{0,4}}{re.escape(num)}号", s)
        return s[m2.end():] if m2 else s

    for i, l in enumerate(lines):
        s = l.strip()
        if not s:
            out.append(l); continue
        if i < 12:
            # ① 标题：剥前缀（公文头可能「[部委] 标题 文号 发文对象」全挤一行）
            if not title_done and title and title in s:
                title_done = True
                # 标题**前面**的部委名（「财政部 税务总局关于…」里的前缀）也是抬头，一并丢
                s = s[s.find(title) + len(title):]
                if not s.strip():
                    continue
            # ② 文号：独立成行则丢，贴在别处则剥 —— 与标题分开判断，互不遮挡
            if not circ_done and s.strip():
                if _is_circ_line(s.strip()):
                    circ_done = True
                    continue
                cut = _cut_circ(s)
                if cut != s:
                    circ_done = True
                    s = cut
                    if not s.strip():
                        continue
        out.append(s)
    return "\n".join(out)


_SORT_PREFIX = re.compile(r"^\d+\s+")          # 文件名常带排序前缀，如「0 关于…的通告」


def doc_no_from_name(stem: str, body: str = "") -> str:
    """定文号。顺序：文件名 → 正文前几行 → **标题兜底**。

    文号务必别取错：文件名开头的「0」「1」是**排序前缀**不是文号；
    正文里的「（税总发[2021]14号）」是**引用别人**，也不是本文文号。
    两者都要排除。真没有文号的公文（如深圳这份通告），只能拿标题当标识 —— 否则
    每条条文会挂个「0」这种毫无意义的出处。
    """
    stem = _SORT_PREFIX.sub("", stem).strip()
    m = _CIRC.match(stem)
    if m:
        return m.group(1).strip()
    for line in (body or "").splitlines()[:6]:          # 正文开头的独立文号行
        s = line.strip()
        if _CIRC.match(s) and len(s) <= 30:
            return _CIRC.match(s).group(1).strip()
    return stem                                          # 无文号 → 标题即标识


# 注：EY 导出件的 Part A 元数据（Type of Tax / Sub-Type / Topic / Issuing Body …）**已弃用**。
# 理由（用户 2026-09-28）：那是 EY 自己的内部口径、不是权威分类，且这批素材很多文件并非 EY 件。
# 分类一律从**条文正文**自行分析；文号/名称取自文件名。


def strikeouts(doc) -> list[str]:
    """红线 = 你画的 StrikeOut 批注（作者 Steven-MZ.Sun）。返回被划文字（归一化）。"""
    out = []
    for pg in doc:
        if not pg.annots():
            continue
        for a in pg.annots():
            if a.type[1] == "StrikeOut":
                t = norm(pg.get_text("text", clip=a.rect))
                if len(t) >= 3:
                    out.append(t)
    return out


def clean_body(text: str) -> list[str]:
    """去页码/页眉页脚噪声，合并版面硬换行（中文会被按版心宽度劈开）。"""
    lines = []
    for l in text.splitlines():
        s = l.strip()
        if not s or _PAGENO.match(s) or s.startswith(_JUNK):
            continue
        lines.append(s)
    merged, buf = [], ""
    for l in lines:
        m = _CLAUSE.match(l)
        if m and m.group(1).startswith("第") and buf and not _SENT_END.search(buf):
            # 「《暂行条例》\n第七条的…」——版面换行把引用劈开了，不是新条，继续合并
            m = None
        if buf and (m or _SENT_END.search(buf)):
            merged.append(buf); buf = l
        else:
            buf = (buf + l) if buf else l
    if buf:
        merged.append(buf)
    return merged


# 条号不一定在行首：PDF 常把「一、…。二、…。」抽成一整行，必须在句中再切一刀。
# 只认「句末标点后的条号」，避免误切正文里引用的「一、」
_INNER = re.compile(
    r"(?<=[。；：:])(?=(?:第[一二三四五六七八九十百零\d]+条|[一二三四五六七八九十百]+、"
    r"|\d+[．.]|[（(][一二三四五六七八九十]+[)）]))")


def cut_clauses(segs: list[str]) -> list[dict]:
    """切条：一、/第X条 为一级；1．/（一） 为二级，归属最近的上级。"""
    exploded = []
    for seg in segs:
        exploded.extend(p for p in _INNER.split(seg) if p.strip())
    segs = exploded
    recs, parent, cur = [], "", None
    for seg in segs:
        m = _CLAUSE.match(seg)
        if m:
            if cur:
                recs.append(cur)
            mk = m.group(1).rstrip("、.．")
            if re.match(r"^(第[一二三四五六七八九十百零\d]+条|[一二三四五六七八九十百]+)$", mk):
                parent = mk
                unit = mk
            else:
                unit = f"{parent}-{mk}" if parent else mk
            cur = {"unit": unit, "marker": m.group(1), "text": seg}
        else:
            if cur:
                cur["text"] += seg
            else:
                recs.append({"unit": "", "marker": "", "text": seg})
    if cur:
        recs.append(cur)
    return recs


def status_of(full_text: str, dead: list[str]) -> str:
    n = norm(full_text)
    if any(d == n for d in dead):
        return "已作废"
    if any(d in n for d in dead):
        return "部分作废"
    return "有效"


def taxes_of(text: str, title_taxes: list[str]) -> tuple[list[str], bool]:
    """条文级税种。返回 (税种列表, 是否来自文件标题兜底)。

    ① 以**条文正文**为准；
    ② 条文明文没写税种时，才用**文件标题**兜底（如「…有关企业所得税税务处理问题的通知」）
       —— 标题是官方名称，不是 EY 的元数据。来源会标出，不静默编造。
    刻意**不用** EY 的 Type of Tax：那是 EY 内部口径，这批素材还很多不是 EY 件。
    """
    hit = [t for t in TAXES if t in text]
    for alias, std in TAX_ALIAS.items():          # 旧称也要认（如「土地使用税」=「城镇土地使用税」）
        if alias in text and std not in hit:
            hit.append(std)
    if hit:
        return hit, False
    return [t for t in title_taxes], bool(title_taxes)


def rules_of(text: str) -> list[str]:
    """一条条文可以属于**多个**处理规则（用户 2026-09-28）。

    如「不征收过户环节的证券交易印花税」既在「产权转移」也在「免征项目」。
    一条都没命中才落「一般规定」。
    """
    hit = [n for n, kws in RULES if any(k in text for k in kws)]
    return hit or ["一般规定"]


def subjects_of(blob: str, table=None) -> list[str]:
    return [n for n, ws in (table or SUBJECTS) if any(w in blob for w in ws)]


def parse_pdf(p: Path) -> dict:
    doc = pymupdf.open(str(p))
    raw = canonic("\n".join(pg.get_text() for pg in doc))
    dead = strikeouts(doc)
    is_ey = "Part B" in raw and "Part A" in raw
    meta_head, rest = split_meta_head(raw)       # 第三种形态：法律数据库导出件
    if is_ey:
        # EY 件只借它的 Part B（正文）；Part A 的元数据**弃用**——
        # 那是 EY 自己的分类口径，不是权威，且非 EY 件根本没有。
        head, part_b = raw.split("Part B", 1)
        body = part_b.split("Part C", 1)[0]
    elif meta_head:
        body = rest                              # 元数据头已剥离，正文里标题/文号还会再出现一次
    else:
        body = strip_web_prefix(raw)
    body = re.split(r"Date of Data|Data entered by|Data\s*reviewed by", body)[0]   # 去 Part C 尾巴
    _stem = _SORT_PREFIX.sub("", p.stem).strip()        # 去排序前缀「0 」「1 」
    circ = doc_no_from_name(p.stem, body)               # 文号：文件名 → 正文 → 标题兜底
    title = _stem[len(circ):].strip(" -_") or _stem     # 名称 = 去掉文号前缀
    # 事件 = 主体 + 事项，**两者都进**（用户 2026-09-28）。必须在条文循环**之前**算好：
    # 条文的兜底主体要用这个合并结果，否则话题永远进不了事件层。
    # 事件两层（用户 2026-09-28「两个都进」）：
    #   ① 主体（单位/辖区）—— 关键词匹配文件名+标题
    #   ② 事项 —— 强特征词匹配正文；弱特征词**只认标题**
    # 通用政策文件（哪层都没命中）才退而用「标题事项」当事件名。
    # 主体：单位关键词（文件名+标题+正文，发文机关常只在正文）
    # + 辖区（只认文件名/标题/所在目录 —— 见 LOCALITY_DIRS 的说明）
    # 单位类：只认文件名+标题（正文绝不能用 —— 见 TOPIC_SUBJECTS 注释）
    # 事项类：可认正文（一条讲股权代持的条文，这份文件就是在讲它）
    subs = subjects_of(p.name + title) + subjects_of(p.name + title + "\n" + body[:1500],
                                                   TOPIC_SUBJECTS)
    loc = locality_hint(p, title)
    if loc and loc not in subs:
        subs = [loc] + subs
    for name, kws in TITLE_TOPICS:
        if any(k in title for k in kws) and name not in subs:
            subs = subs + [name]
    if not subs:
        topic = title_topic(title)
        if topic:
            subs = [topic]
    main, annex = split_annex(body)
    title_taxes = [t for t in TAXES if t in p.stem]      # 文件标题里点名的税种（官方名称）
    title_taxes += [v for k, v in TAX_ALIAS.items() if k in p.stem and v not in title_taxes]
    merged = clean_body(main)                            # 先合并版面硬换行
    # 抬头/文号要在**合并之后**剥：标题常被版心劈成两行（…完税凭证查验服 / 务工作的通告），
    # 按行比对会失效；合并后标题才完整。
    zone = min(3, len(merged))
    merged = [x for x in (strip_header_lines(s, title, circ) for s in merged[:zone])
              if x.strip()] + merged[zone:]
    recs = cut_clauses(merged)
    for r in recs:
        r["status"] = status_of(r["text"], dead)
        r["taxes"], r["tax_from_title"] = taxes_of(r["text"], title_taxes)
        r["rules"] = rules_of(r["text"])

    # 兜底第三层：发文对象/定义/程序这类条款本身不点税种，但它所属文件通篇讲哪些税，
    # 它就属于那些税 —— 不该被孤立到"待归类"（用户 2026-09-28：发文对象界定适用对象、
    # 定义解释概念、程序要与实体在一起）。
    doc_taxes = [t for r in recs for t in r["taxes"]]
    doc_taxes = list(dict.fromkeys(doc_taxes))
    for r in recs:
        if not r["taxes"] and doc_taxes:
            r["taxes"], r["tax_from_title"] = doc_taxes, True
        # 适用主体：条文里点名的优先，否则用文件层面的（这批是"特定单位"专用件）
        r["subjects"] = subjects_of(r["text"], TOPIC_SUBJECTS) or subs
    # 事件 = 主体 + 事项，**两者都进**（用户 2026-09-28）。
    # 如「上海市…地方教育附加征收管理办法」既属事件「上海市」，也属「地方教育附加」，
    # 这样"某事项跨地区分别怎么规定"才查得到。通用全国文件没有主体，就只有事项。
    return {
        "file": p.name, "sha8": hashlib.sha256(p.read_bytes()).hexdigest()[:8],
        "doc_no": circ, "doc_name": title, "ey": is_ey,
        "issuer": meta_head.get("发文机关", ""),
        "effective": meta_head.get("生效日期", ""),
        "validity": meta_head.get("时效性", ""),
        "clauses": recs, "annex": annex,
        "subjects": subs,     # 主体 + 事项合并（两者都进事件层）
        "dead_count": sum(1 for r in recs if r["status"] != "有效"),
    }


def line_of(r: dict, doc_no: str, subjects: list[str] | None = None) -> str:
    """一条条文一行。

    必须带**适用主体**：这批是特定单位专用规则，脱离主体就会被当成通用税法误用。
    """
    lab = r["unit"] or "前言"
    tag = {"已作废": " 「已作废」", "部分作废": " 「部分作废」"}.get(r["status"], "")
    who = subjects if subjects is not None else r.get("subjects") or []
    scope = f"  〔仅适用：{'/'.join(who)}〕" if who else "  〔适用主体未识别〕"
    src = "  〔税种依文件标题〕" if r.get("tax_from_title") else ""
    return f"【{doc_no} {lab}】{r['text']}{tag}{scope}{src}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="素材目录（递归取 *.pdf）")
    ap.add_argument("--files", nargs="*", default=[], help="直接指定文件（给出时忽略 --src）")
    ap.add_argument("--files-from", default="", help="从文件读路径清单（每行一个）。"
                                                     "**文件名常带空格，用这个而不是 shell 展开**")
    ap.add_argument("--out", default=str(Path.home() / "ng-regs"))
    ap.add_argument("--batch", default="", help="批次名（默认取源目录名）。**每批独立存放，避免互相覆盖**；"
                                                "最终跨批合并是单独一步")
    a = ap.parse_args()

    if a.files_from:
        docs = [Path(x.strip()) for x in Path(a.files_from).read_text(encoding="utf-8").splitlines()
                if x.strip() and not x.strip().startswith("#")]
        src_label = f"清单 {Path(a.files_from).name}"
        batch = a.batch or Path(a.files_from).stem
    elif a.files:
        docs = [Path(f) for f in a.files]
        src_label = "、".join(p.parent.name for p in docs[:1])
        batch = a.batch or (docs[0].parent.name if docs else "batch")
    elif a.src:
        src = Path(a.src)
        src_label = str(src)
        docs = sorted(p for p in src.rglob("*.pdf") if not p.name.startswith("._"))
        batch = a.batch or src.name
    else:
        ap.error("需要 --src 或 --files")
    out = Path(a.out) / batch
    # 批次是**整体重算**的：先清空该批次目录，否则上轮的产物会残留成过期文件
    # （实测：改了主体判定后，旧运行留下的「军队军工.md」（92 条）仍在目录里，
    #   看着像还在误判，实际是脏数据）。
    if out.exists():
        import shutil
        shutil.rmtree(out)
    print(f"扫描 {len(docs)} 份 PDF …（{src_label}）→ 批次「{batch}」")

    parsed, errors = [], []
    for i, p in enumerate(docs, 1):
        try:
            parsed.append(parse_pdf(p))
        except Exception as e:      # noqa: BLE001
            errors.append((p.name, f"{type(e).__name__}: {e}"))
        if i % 10 == 0:
            print(f"  {i}/{len(docs)}")

    # ---- 文号消歧（用户 2026-09-28 方案A）----
    # 同一文号可能被多份文件共用（实测：深圳「公告2018年第2号」被 10 份共用，
    # 是 2018 年规范性文件批量修正时共用的公告号）。此时光有文号指不出唯一文件，
    # 【公告2018年第2号 第三条】是歧义的。**只在重复处**补短标题，其余保持原样。
    _counts = collections.Counter(d["doc_no"] for d in parsed)
    for d in parsed:
        if _counts[d["doc_no"]] > 1:
            d["display_no"] = f"{d['doc_no']}·{short_title(d['doc_name'])}"
            d["ambiguous"] = True
        else:
            d["display_no"] = d["doc_no"]
            d["ambiguous"] = False

    # ---- 附件（表格/目录）：不参与条文层，但**单独留存**，不悄悄丢 ----
    annex_docs = [d for d in parsed if d["annex"].strip()]
    for d in annex_docs:
        f = out / "附件" / f"{d['doc_no']}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            f"# {d['doc_no']} 附件\n\n"
            f"> 原件中的表格/目录（如免税目录、装备清单），**非条文**，条文层不收录。\n"
            f"> 源文件：{d['file']}\n\n```\n{d['annex']}\n```\n", encoding="utf-8")

    # ---- 第 1 层：按税种 / 处理规则 ----
    tax_rule: dict[tuple, list] = collections.defaultdict(list)
    # ---- 第 2 层：按事件 ----
    by_event: dict[str, list] = collections.defaultdict(list)
    # ---- 待归类 ----
    unclassified = []
    no_subject = []      # 有税种归属、仅主体未识别
    no_tax = []          # 有主体归属、条文未涉税（如商事登记类程序条款）

    for d in parsed:
        for r in d["clauses"]:
            if not r["unit"] and len(r["text"]) < 12:
                continue                     # 纯噪声行
            # 事件层：条文级主体 > 文件级主体 > 标题事项（通用政策文件）
            evs = r["subjects"] or d["subjects"] or []
            body = line_of(r, d["display_no"], subjects=evs)
            if r["taxes"]:
                for t in r["taxes"]:
                    for rule in r["rules"]:          # ← 一条可归入多个处理规则
                        tax_rule[(t, rule)].append((d["display_no"], body))

            else:
                # 不涉税不等于"没归属"：有主体的条文已在事件层落了户（用户 2026-09-28：
                # 程序要与实体在一起）。只有**税种和主体都没有**才算真待归类。
                if not evs:
                    unclassified.append((d["display_no"], body, "税种与主体都未识别"))
                else:
                    no_tax.append((d["display_no"], body))
            for s in evs:
                by_event[s].append((d["display_no"], r, body))
            # 待归类**只收真正没归属的**：已有税种分类的条文不算待归类，
            # 只是主体没识别出来 —— 单独统计，不混进待归类（否则报告误导）。
            if not r["taxes"] and not evs:
                unclassified.append((d["display_no"], body, "税种与主体都未识别"))
            elif not evs:
                no_subject.append((d["display_no"], body))

    # 写第 1 层
    for (t, rule), rows in sorted(tax_rule.items()):
        f = out / "按税种规则" / t / f"{rule}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {t} · {rule}\n",
                 f"> 来自 {len({r[0] for r in rows})} 份法规，共 {len(rows)} 条条文\n"]
        cur_no = None
        for no, body in rows:
            if no != cur_no:
                lines.append(f"\n## {no}\n"); cur_no = no
            lines.append(f"- {body}\n")
        f.write_text("\n".join(lines), encoding="utf-8")

    # 写第 2 层（跨税种组合：事件 → 按税种分组）
    for subj, rows in sorted(by_event.items()):
        f = out / "按事件" / f"{subj}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        taxes = sorted({t for _, r, _ in rows for t in r["taxes"]})
        lines = [f"# {subj}\n",
                 f"> 跨 {len(taxes)} 个税种：{' · '.join(taxes)}",
                 f"> 来自 {len({n for n, _, _ in rows})} 份法规，共 {len(rows)} 条条文\n"]
        def _section(title: str, pred) -> None:
            group = [(no, r, body) for no, r, body in rows if pred(r)]
            if not group:
                return
            lines.append(f"\n## 【{title}】\n")
            cur_no = None
            for no, r, body in group:
                if no != cur_no:
                    lines.append(f"\n### {no}\n"); cur_no = no
                lines.append(f"- {body}\n")

        for t in taxes:
            _section(t, lambda r, t=t: t in r["taxes"])
        # 不涉税的条文（如商事登记类程序条款）也要落在这里 ——
        # 否则事件文件头写着"共 N 条"，正文却少了一截，看着像丢了。
        _section("未涉税（同属该主体的其他规定）", lambda r: not r["taxes"])
        f.write_text("\n".join(lines), encoding="utf-8")

    # ---- 索引 ----
    total = sum(len(d["clauses"]) for d in parsed)
    dead = sum(d["dead_count"] for d in parsed)
    idx = [f"# 法规知识库索引\n",
           f"> 生成 {datetime.date.today()} · 来源 {src_label}\n",
           f"| 项 | 值 |", f"|---|---|",
           f"| 法规文件 | {len(parsed)} 份（原生公文 {sum(1 for d in parsed if not d['ey'])} 份）|",
           f"| 条文本单元 | {total} 条 |",
           f"| 作废/部分作废 | {dead} 条 |",
           f"| 事件主体 | {len(by_event)} 个 |",
           f"| 含附件（单独留存）| {len(annex_docs)} 份 |",
           f"| 税种×规则 分类 | {len(tax_rule)} 个 |",
           f"| 解析失败 | {len(errors)} 份 |", ""]
    idx.append("## 文号清单\n")
    idx.append("| 文号 | 名称 | 条文 | 作废 | 事件 |")
    idx.append("|---|---|---|---|---|")
    for d in parsed:
        idx.append(f"| {d['display_no']} | {d['doc_name'][:28]} | {len(d['clauses'])} | "
                   f"{d['dead_count']} | "
                   f"{'/'.join(d['subjects']) or '—'} |")
    if no_tax:
        idx.append(f"\n## 未涉税条文（{len(no_tax)} 条，已归入事件层）\n")
        for no, body in no_tax[:30]:
            idx.append(f"- {body[:100]}")
    if no_subject:
        idx.append(f"\n## 主体未识别（{len(no_subject)} 条，已有税种归属）\n")
        for no, body in no_subject[:40]:
            idx.append(f"- {body[:100]}")
    if unclassified:
        idx.append(f"\n## 待归类（{len(unclassified)} 条）\n")
        for no, body, why in unclassified[:60]:
            idx.append(f"- `{why}` {body[:90]}")
    if errors:
        idx.append(f"\n## 解析失败（{len(errors)} 份）\n")
        for n, e in errors:
            idx.append(f"- {n} — {e}")
    (out / "_索引.md").write_text("\n".join(idx), encoding="utf-8")

    print(f"\n✓ 完成")
    print(f"  条文 {total} 条 | 作废 {dead} 条 | 事件 {len(by_event)} 个 | 分类 {len(tax_rule)} 个")
    print(f"  失败 {len(errors)} 份 | 待归类 {len(unclassified)} 条")
    print(f"  输出 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
