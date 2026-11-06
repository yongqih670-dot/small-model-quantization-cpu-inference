"""Draw the selective-classification and paired-latency figures from result tables."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

def setup():
    installed={f.name for f in font_manager.fontManager.ttflist}
    font=next((f for f in ["Microsoft YaHei","Noto Sans CJK SC","SimHei"] if f in installed),None)
    if not font:
        raise SystemExit("A Chinese font is required")
    plt.rcParams.update({"font.family":font,"axes.unicode_minus":False,"axes.spines.top":False,"axes.spines.right":False})

def classification(p,s):
    fig,ax=plt.subplots(figsize=(7.8,2.8),layout="constrained")
    keys=list(s); left=[0]*len(keys)
    cats=[("自动接受且答对","#3b7987"),("自动接受但答错","#c56942"),("转交人工","#cbd2d3")]
    for col,(label,color) in enumerate(cats):
        vals=[(v["accepted"]-v["missed"],v["missed"],v["review"])[col] for v in s.values()]
        bars=ax.barh(keys,vals,left=left,label=label,color=color,height=.6)
        ax.bar_label(bars,label_type="center",color="white" if col<2 else "#273e46")
        left=[x+y for x,y in zip(left,vals)]
    ax.set_xlabel("新题数（每个模型 30 题）");ax.set_xlim(0,30);ax.invert_yaxis()
    ax.legend(loc="lower left",bbox_to_anchor=(0,1.02),ncols=3,frameon=False)
    fig.savefig(p/"H_review.png",dpi=150);plt.close(fig)

def latency(p,s):
    with (p/"I_new_pairs.csv").open(encoding="utf-8") as f:
        pairs=list(csv.DictReader(f))
    fig,axes=plt.subplots(1,3,figsize=(9.7,3.55),sharey=True,layout="constrained")
    for ax,(key,title) in zip(axes,[("story","叙事"),("manual","流程说明"),("minutes","会议记录")]):
        cells=sorted((r for r in s["cells"] if r["text"]==key),key=lambda r:r["g"])
        for r in pairs:
            if r["text"]==key:
                ax.scatter(int(r["g"])+(int(r["round"])-3.5)*.72,float(r["total"]),s=17,color="#80949a")
        ax.plot([r["g"] for r in cells],[r["total"] for r in cells],"o-",color="#17617a",lw=1.6,ms=4)
        ax.axhline(0,ls="--",lw=.9,color="#454c4f");ax.set_xticks([16,32,48]);ax.set(title=title,xlabel="输出 token 数")
        ax.grid(axis="y",alpha=.15)
    axes[0].set_ylabel("Q8 减 Q4（ms，正值表示 Q4 更快）")
    fig.suptitle("768 token 新文本的轮内延迟差：散点为各轮，折线为均值")
    fig.savefig(p/"I_texts.png",dpi=150);plt.close(fig)

if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("results",type=Path);p=ap.parse_args().results
    s=json.loads((p/"summary.json").read_text(encoding="utf-8"))
    setup();classification(p,s["H"]["new"]);latency(p,s["I"]["new"])
