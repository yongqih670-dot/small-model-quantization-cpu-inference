"""Create the figures shown in the report using the processed CSV/JSON tables."""
import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

HERE=Path(__file__).resolve().parent
COLORS={"Q4_K_M":"#196078","Q8_0":"#bd593a"}

def config():
    names={font.name for font in font_manager.fontManager.ttflist}
    fonts=[f for f in ("Microsoft YaHei","Noto Sans CJK SC","SimHei") if f in names]
    if not fonts:
        raise RuntimeError("Install a Chinese font (Microsoft YaHei, Noto Sans CJK SC, or SimHei)")
    plt.rcParams.update({"font.family":fonts[0],"axes.unicode_minus":False,"font.size":10,"axes.spines.right":False,
                         "axes.spines.top":False,"figure.facecolor":"white","savefig.facecolor":"white"})
def crossover(out,s):
    fig,(ax,delta)=plt.subplots(1,2,figsize=(10,3.8),layout="constrained")
    for k,c in COLORS.items():
        cells=[v for v in s["D"]["cells"] if v["model"]==k]
        ax.errorbar([v["output"] for v in cells],[v["mean"] for v in cells],
                    yerr=[v["sd"] for v in cells],label=k,lw=1.8,marker="o",ms=4,capsize=3,color=c)
    ax.set(title="平均端到端延迟",ylabel="毫秒（均值 ± 样本标准差）")
    ax.legend(frameon=False)
    with (out/"D_paired.csv").open(encoding="utf-8") as f: rows=list(csv.DictReader(f))
    for r in rows:
        delta.scatter(int(r["output"])+(int(r["round"])-3.5)*.7,float(r["Q8_minus_Q4_ms"]),
                      s=18,color="#83949b",alpha=.75)
    diff=s["D"]["difference"]
    delta.plot([r["output"] for r in diff],[r["mean"] for r in diff],"o-",label="配对差均值",color="#214b60")
    delta.axhline(0,color="#424b4e",lw=.8,ls="--")
    delta.set(title="每个点代表一轮配对",ylabel="Q8 减 Q4（ms，正值表示 Q4 更快）")
    delta.legend(frameon=False)
    for a in (ax,delta):
        a.set_xticks([16,32,48,64,96]);a.set_xlabel("固定生成的 token 数");a.grid(axis="y",alpha=.14)
    fig.savefig(out/"D_latency.png",dpi=155);plt.close(fig)
def thread_maps(out,s):
    fig,axes=plt.subplots(1,2,figsize=(9.0,4.15),layout="constrained")
    ticks=[4,8,16]
    for ax,(n,g) in zip(axes,[(768,16),(64,96)]):
        vals={(r["t"],r["tb"]):r["mean"] for r in s["F"] if (r["input"],r["output"])==(n,g)}
        grid=[[vals[t,b] for b in ticks] for t in ticks]
        im=ax.imshow(grid,cmap="Blues",vmin=min(vals.values())*.92,vmax=max(vals.values())*1.02)
        for i,t in enumerate(ticks):
            for j,b in enumerate(ticks):
                val=vals[t,b]
                ax.text(j,i,f"{val:.0f}",ha="center",va="center",
                        color="white" if im.norm(val)>.55 else "#163648",weight="bold" if val==min(vals.values()) else "normal")
        ax.set_xticks(range(3),labels=ticks);ax.set_yticks(range(3),labels=ticks)
        ax.set(xlabel="批处理线程数 (-tb)",ylabel="生成线程数 (-t)",title=f"输入 {n} → 输出 {g} token")
    fig.suptitle("平均端到端延迟（ms；各图独立色阶）")
    fig.savefig(out/"F_threads.png",dpi=155);plt.close(fig)
if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("results",type=Path)
    out=ap.parse_args().results
    s=json.loads((out/"summary.json").read_text(encoding="utf-8"))
    config();crossover(out,s);thread_maps(out,s)
