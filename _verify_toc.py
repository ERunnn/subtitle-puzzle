import json, re, subprocess, urllib.request
from pathlib import Path

s = Path("README.md").read_text(encoding="utf-8")
lines = s.splitlines()
heads = [l.lstrip("# ").strip() for l in lines if re.match(r"^#{1,3} ", l)]

def slug(t):
    # 保留：数字、字母、CJK（U+4E00–U+9FFF）、空白、连字符
    return re.sub(r"[^0-9a-z一-鿿\s-]", "", t).strip().lower().replace(" ", "-")

anchors = [a for a in re.findall(r'(?:href="|\]\()#([^")]+)', s) if not a.startswith("http")]
miss = [a for a in anchors if a not in [slug(h) for h in heads]]
print("锚点:", len(anchors), "| 对不上:", miss or "无")
assert not miss

# 目录：应无 details/nested，平铺 7 项
toc = s[s.index("## 目录"):s.index("## 项目简介")]
print("目录含 details:", "<details" in toc, "| 目录含 <ul>:", "<ul" in toc)
assert "<details" not in toc and "<ul" not in toc
assert toc.count("](#") == 7, f"目录 markdown 链接数应为 7，实际 {toc.count('](#')}"
print("目录平铺项数: 7（含功能介绍，不含其二级项）")

# 正文仍保留二级标题
body_er = s[s.index("## 功能介绍"):s.index("## 参数速查")]
print("正文保留 ### 自动捕获:", "### 自动捕获" in body_er, "| ### 输出比例:", "### 输出比例" in body_er)
assert "### 自动捕获" in body_er and "### 输出比例" in body_er

# 图片
refs = sorted(set(re.findall(r'docs/[A-Za-z0-9_./-]+\.(?:png|jpg|jpeg)', s)))
print("图片:", len(refs), "| 缺失:", [r for r in refs if not Path(r).exists()] or "无")
assert not [r for r in refs if not Path(r).exists()]

def token():
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=30).stdout
    for l in out.splitlines():
        if l.startswith("password="):
            return l.split("=", 1)[1].strip()

req = urllib.request.Request("https://api.github.com/markdown", method="POST",
    data=json.dumps({"text": s, "mode": "gfm", "context": "ERunnn/subtitle-puzzle"}).encode())
req.add_header("Authorization", f"token {token()}")
req.add_header("Content-Type", "application/json")
req.add_header("User-Agent", "md-check")
html = urllib.request.urlopen(req, timeout=90).read().decode()
seg = html[html.find("目录</h2>"):html.find("项目简介</h2>")]
print("GitHub 渲染: details=", seg.count("<details"), "| <li>=", seg.count("<li"),
      "| 7 链接=", seg.count("<a href=\"#") == 7)
assert seg.count("<details") == 0 and seg.count("<li") == 7 and seg.count("<a href=\"#") == 7

# CHANGELOG
q = Path("CHANGELOG.md"); t = q.read_text(encoding="utf-8")
anchor = "- GitHub 账号由 `ERun-3` 改名为 `ERunnn`：16 处链接（README / 落地页 / llms.txt / release.py）、Pages 域名、git remote 与提交邮箱同步"
assert t.count(anchor) == 1
extra = ("\n- 目录恢复为平铺列表：移除 `功能介绍` 的嵌套折叠，二级项（自动捕获 / 输出比例）"
         "不再出现在目录，正文二级标题保留")
q.write_text(t.replace(anchor, anchor + extra), encoding="utf-8")
print("CHANGELOG 已更新")

def run(*a):
    return subprocess.run(a, capture_output=True, text=True, timeout=180)

run("git", "add", "-A")
print("commit:", run("git", "commit", "-q", "-m", "docs: 目录恢复平铺列表，移除二级项折叠").returncode)
run("git", "tag", "-f", "-a", "v1.1.0", "-m", "台词匠 v1.1.0")
for i in range(3):
    a = run("git", "push", "origin", "main")
    b = run("git", "push", "-f", "origin", "v1.1.0")
    print(f"push #{i+1}: main={a.returncode} tag={b.returncode}")
    if not run("git", "log", "--oneline", "origin/main..HEAD").stdout.strip():
        break
print("提交:", run("git", "log", "--oneline", "-1").stdout.strip())
print("未推送:", run("git", "log", "--oneline", "origin/main..HEAD").stdout.strip() or "无")
print("工作区:", run("git", "status", "--short").stdout.strip() or "干净")
