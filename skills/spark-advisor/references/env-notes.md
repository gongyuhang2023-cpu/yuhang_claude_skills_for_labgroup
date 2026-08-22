# ARM64 (aarch64) 生态笔记 — 参考机 DGX Spark

> 这台服务器是 **aarch64 / ARM64** 架构（不是 x86）。在上面装或跑程序前先看这里，省得进去踩坑。
> **实时事实**（当前 CUDA/Python/你有哪些 venv）看自动探测的 `env-profile.local.json`；
> **本文件是耐用的经验知识**（哪些包能在 ARM 上装、坑在哪），手工维护。
> 开源给 x86 服务器的人请替换本文件。

## 架构与底座
- **aarch64**，DGX OS（Ubuntu 24.04 基底），定制内核 `6.17-nvidia`
- GPU：GB10（统一内存，无独立显存框），驱动 580，**CUDA 13.0**
- 系统 Python 3.12、gcc 13.3（可从源码编译）、**docker 可用**
- **系统无 conda/mamba** —— 要 conda 得自己在家目录装 miniforge

## 关键：哪些包在 aarch64 上能装（决定能不能全速跑）

**已确认有原生 aarch64 wheel（全速）**（部署摸底时 pip 探测过）：
- `torch`（aarch64 + CUDA sbsa 构建）、`triton`、`cuequivariance`(-ops-torch-cu13)、`boltz`
- → **Boltz 全链条在 ARM 上原生全速**，是这台机的主力用途，没问题。

**尚未验证（用到再测，别假设有）**：
- `fpocket`、`rdkit=2020.09`（老版本）、`unidock`
- 原因：这些常走 conda-forge，而系统没 conda；aarch64 可得性没实测过。
- 做法：真要用时先在家目录装 miniforge，再 `conda search --platform linux-aarch64 <pkg>` 确认；或找预编译 aarch64 包 / 用 gcc 13.3 自己编译。

## 生信工具链（细菌基因组注释）—— 2026-08-06 实测

**结论先行：aarch64 对这条链基本不是障碍。整个 conda 环境 44 秒装成，13 个工具逐个 `--version` 实测可跑。**

> ★ 这条**订正了一个错误预期**。动手前的判断是"prokka/bakta 这类可能在 aarch64 上装不上"，
> 并据此把"环境风险"列为不上服务器的理由之一。**实际不成立** —— 主流生信工具要么是
> **noarch（纯 Python）**、要么**有原生 aarch64 二进制**。真正的限制出现在一个完全没预料到的地方（见下）。
> 下次别再用"aarch64 装不上"当劝退理由，先查 `subdir`。

**✅ noarch（纯 Python，跨平台可装）**：`bakta` · `panaroo` · `dbcan` · `kofamscan` · `checkm2`

**✅ 有原生 linux-aarch64 构建**：`prodigal` · `hmmer` · `diamond` · `mmseqs2` · `blast` · `seqkit` ·
`aragorn` · `infernal` · `tRNAscan-SE` · `ncbi-datasets-cli` · `csvtk` · `ncbi-amrfinderplus`

**❌ 唯一撞到的真实限制**：`bakta 1.12.0` 依赖 `blast>=2.17.0`，而 **aarch64 没有该版本的构建**
（只有 linux-64）→ mamba 直接解不出来。**用 `bakta 1.11.4`。**

**⚠️ `bakta 1.11.4` 必须钉 `pyhmmer<0.11`**（实测 `0.10.15` 可用）。
`pyhmmer 0.12.x` 把 `hit.name` 从 `bytes` 改成 `str`，bakta 会在
`orf.detect_spurious()` / `cds.predict_pfam()` 抛 `AttributeError: 'str' object has no attribute 'decode'` 崩掉。
症状具迷惑性：**CDS 已经预测完了**才崩。

**⚠️ 避开 `prokka`**：其 `tbl2asn` 历来只有 x86 二进制。功能上 `bakta` 是它的现代替代。

**查一个包能不能装的正确姿势**（别只看有没有 `linux-aarch64`）：
```bash
# noarch 的包在任何架构都能装 —— 只看 aarch64 会把纯 Python 包误判成"没有"
curl -s https://api.anaconda.org/package/bioconda/<pkg> | python -c "
import sys,json; d=json.load(sys.stdin)
print(d['latest_version'], sorted({f.get('attrs',{}).get('subdir') for f in d['files']}))"
```

## 建议用法
- **每人在自己家目录建 venv 或 conda 环境**（隔离、钉死依赖）；别裸依赖系统 Python。
- 装包优先 pip wheel → 没 wheel 再考虑 conda(自装 miniforge) 或源码编译。
- 需要复杂/x86-only 依赖时可考虑 docker（但要 ARM 镜像）。

## 陈旧提示
本文件是手工经验，`env-profile.local.json` 是自动实时探测。两者冲突以实时探测为准，并回来更新本文件。
