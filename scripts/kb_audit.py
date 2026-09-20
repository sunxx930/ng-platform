#!/usr/bin/env python3
"""知识库全面审计：扫描所有条目里可能残留的未脱敏信息。

重点盯「非客户条目」（法规实务/方法论）——它们不走客户脱敏，
是广发控股那种「客户文档被当成通用材料入库」的高发区。
"""
import json, re, sys
from pathlib import Path

ROOT = Path.home() / '.ng-platform' / 'knowledge' / 'tax-cases'
MAP = json.loads((Path.home() / '.ng-platform' / '_client_map.json').read_text(encoding='utf-8'))
WATCH = set(json.loads((Path.home() / '.ng-platform' / '_person_watch.json').read_text(encoding='utf-8')))
MASKED = set()
for v in MAP.values():
    MASKED.update(a for a in v.split('/') if a)

# 通用/句式噪声（不是真实主体）
GENERIC = re.compile(
    r'^(以上|以下|该|本|相关|上述|某|各|母|子|关联|目标|中国|香港|境外|境内|全体|部分|其他|任何|一家|两家|多家|'
    r'是英国|是|从事|通过|设立|变更为|成立了|原判认定|并将|全体股东|公司全|国际|有限责任|股份有限|'
    r'科技|投资|管理|咨询|服务|贸易|实业|发展|控股|集团|中心|协会|大学|医院|银行)')

CN_CO = re.compile(r'[一-龥（）()]{2,22}(?:有限公司|股份有限公司|有限责任公司|集团有限公司|合伙企业（有限合伙）|合伙企业)')
EN_CO = re.compile(r"\b([A-Z][A-Za-z&.'\-]+(?:\s+[A-Z][A-Za-z&.'\-]+){0,4})\s+"
                   r"(Ltd\.?|Limited|Inc\.?|Corp\.?|Corporation|Company|Holdings?|Group|Capital|Partners?|LLC|PLC|GmbH|B\.?V\.?|N\.?V\.?)\b")
AMOUNT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|千元)')
ADDR = re.compile(r'[一-龥]{2,12}(?:路|大道|街|巷|弄)[一-龥]{0,6}?\s*\d{1,5}\s*号')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9-]{2,}(?:\s*\.\s*[A-Za-z0-9-]+)*')
PHONE = re.compile(r'(?:\+86[\s-]?)?1[3-9]\d[\s-]?\d{4}[\s-]?\d{4}|\b0\d{2,3}[\s-]?\d{3,4}[\s-]?\d{4}\b')


def audit():
    findings = {}
    for f in sorted(ROOT.glob('*.md')):
        t = f.read_text(encoding='utf-8')
        head = t[:300]
        is_client = 'type: 筹划案' in head
        body = t
        issues = []

        for m in CN_CO.finditer(body):
            n = m.group(0)
            if n in MASKED or GENERIC.match(n):
                continue
            # 前面紧跟着代号的 = 品牌已掩、通用尾巴留下，不是泄露
            prev = body[max(0, m.start() - 8):m.start()]
            if re.search(r'((公司|个人)[A-Z]{1,2}|\[事务所\]|\[数据库\]|\[实体\])$', prev):
                continue
            issues.append(('中文公司名', n))
        for m in EN_CO.finditer(body):
            n = f'{m.group(1)} {m.group(2)}'
            if any(a.lower() == n.lower() for a in MASKED):
                continue
            if re.match(r'^(The|This|Our|A|An|Retail|Company|Global)\b', n):
                continue
            issues.append(('英文公司名', n))
        for m in AMOUNT.finditer(body):
            issues.append(('精确金额', m.group(0)))
        for m in ADDR.finditer(body):
            issues.append(('地址', m.group(0)))
        for m in EMAIL.finditer(body):
            issues.append(('邮箱', m.group(0)))
        for m in PHONE.finditer(body):
            issues.append(('电话', m.group(0)))
        for n in WATCH:
            if n in body:
                issues.append(('人名', n))
        # 未知人名启发式：职衔后紧跟的中文名（审计盲区——只查已知名单会漏）
        SURNAME = set('赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻潘葛范彭鲁韦马苗凤花方俞任袁柳唐罗薛伍余米贝姚孟顾尹江钟卢汪石戴崔贾龚程陆裴宋庞熊纪舒屈项祝梁杜阮蓝季路娄危童颜郭梅盛林徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫房缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚邢滑裴陸荣翁荀羊惠甄曲家封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公')
        for m in re.finditer(r'(?:合伙人|总经理|总监|董事|经理|先生|女士|老师|博士|顾问)\s*[:：]?\s*([一-龥]{2,4})', body):
            n = m.group(1)
            if n in WATCH or n[0] not in SURNAME:      # 首字必须是常见姓氏，否则是碎片
                continue
            if len(n) == 2 and n[1] not in '先生女士总':
                continue
            issues.append(('可能人名', n))

        if issues:
            # 去重保序
            seen, uniq = set(), []
            for kind, v in issues:
                k = (kind, v)
                if k in seen:
                    continue
                seen.add(k)
                uniq.append((kind, v))
            findings[f.name] = (is_client, uniq)
    return findings


if __name__ == '__main__':
    res = audit()
    client = {k: v for k, v in res.items() if v[0]}
    general = {k: v for k, v in res.items() if not v[0]}
    print(f'扫描 {len(res)} 份有发现（客户案 {len(client)} / 非客户 {len(general)}）\n')
    print('=' * 78)
    print('【非客户条目】← 重点，这些不走客户脱敏')
    print('=' * 78)
    for k, (_, iss) in general.items():
        by = {}
        for kind, v in iss:
            by.setdefault(kind, []).append(v)
        print(f'\n{k}')
        for kind, vs in by.items():
            print(f'   {kind}: {" | ".join(vs[:6])}{" …" if len(vs) > 6 else ""}')
    print('\n' + '=' * 78)
    print('【客户案】← 只列「疑似真实名」的（排除代号/占位符）')
    print('=' * 78)
    for k, (_, iss) in client.items():
        by = {}
        for kind, v in iss:
            by.setdefault(kind, []).append(v)
        print(f'\n{k}')
        for kind, vs in by.items():
            print(f'   {kind}: {" | ".join(vs[:6])}{" …" if len(vs) > 6 else ""}')
