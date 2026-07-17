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

## 建议用法
- **每人在自己家目录建 venv 或 conda 环境**（隔离、钉死依赖）；别裸依赖系统 Python。
- 装包优先 pip wheel → 没 wheel 再考虑 conda(自装 miniforge) 或源码编译。
- 需要复杂/x86-only 依赖时可考虑 docker（但要 ARM 镜像）。

## 陈旧提示
本文件是手工经验，`env-profile.local.json` 是自动实时探测。两者冲突以实时探测为准，并回来更新本文件。
