import httpx

resp = httpx.get("https://qt.gtimg.cn/q=sh600519,sz000001,sz300750", timeout=15)
resp.encoding = "gbk"
lines = [l for l in resp.text.split(";") if "=" in l]
first = lines[0].split("~")
for idx, val in enumerate(first):
    if any(k in str(val) for k in ["酒", "银行", "电池", "电源", "食品"]):
        print(idx, "->", val)
