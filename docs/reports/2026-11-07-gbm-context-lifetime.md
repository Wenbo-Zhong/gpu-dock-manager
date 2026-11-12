# 2026-11-07：独立复现 GBM 上下文探测的残留引用

## 实验范围

使用新增 `tools/gbm_context_probe.py`，在普通用户独立子进程中重复 Mutter `detect_hardware_rendering()` 的主要调用链：GBM 平台显示、GLES2 无配置上下文、无 surface 的 make-current、GL_RENDERER 查询，以及对应清理。
另设不创建上下文的 GBM 显示初始化对照。两次实验均有 15 秒子进程期限，成功退出，无 stderr；所有有返回值的清理调用成功。

本机 NVIDIA 开源内核模块与用户态驱动为 595.99.02，目标 PCI 为 `0000:03:00.0`，GL_RENDERER 返回 NVIDIA RTX PRO 5000 72GB Blackwell/PCIe/SSE2。
不创建窗口或 surface，不绘制，不请求模式设置，不向 GNOME 注入代码或关闭其 fd。实验结束后 GNOME 服务 active，主进程仍为实验开始前的同一进程，启动覆盖为空。

## 结果

下表只计算 NVIDIA 设备节点和 renderD129，不计探针自行打开、随后关闭的 card0 主节点。

| 阶段 | 无 context 对照 | 含 context 探测 |
| --- | ---: | ---: |
| 加载 EGL/GBM/GLES 库后 | 0 | 0 |
| 创建 GBM 设备后 | 0 | 0 |
| 获取 GBM 平台 EGLDisplay 后 | 6 | 6 |
| initialize 后 | 8 | 8 |
| 创建 GLES2 context 后 | — | 14 |
| make-current 后 | — | 20 |
| GL_RENDERER 查询后 | — | 20 |
| unbind、destroy-context 后 | — | 20 |
| terminate、GBM destroy、card0 close 后 | 8 | 20 |
| release-thread 后 | 8 | 14 |
| 清理结束一秒后 | 8 | 14 |

包含 context 的探针在 make-current 后拥有 4 个 nvidiactl、12 个 nvidia0、2 个 renderD129、2 个 nvidia-modeset fd。这与上一轮 GNOME 在对应区间后的节点数量分布一致。
因此先前未覆盖的新增 12 个 fd，已在独立的同类调用链中复现为 create-context 增加 6 个、make-current 再增加 6 个。该结果有力支持临时硬件检测链路的解释，但没有把 GNOME 中每个 fd 逐一映射到驱动内部对象。

release-thread 后关闭了 5 个 nvidia0 和 1 个 nvidia-modeset，仍有 4 个 nvidiactl、7 个 nvidia0、2 个 renderD129、1 个 nvidia-modeset。进程存活时仍占用目标，不能据此拔卡，也不能单凭保留 fd 认定驱动泄漏。

## 对实现的影响

单独清理 `gpu_datas` 不够；正常 EGL/GBM/context 销毁也未在本机探针中完整关闭设备访问。
`eglReleaseThread` 有部分作用，但不能直接插入运行中 GNOME 的主线程作为修复：那里的当前上下文还可能属于 AMD，需要协调并恢复其状态；即使这样，本次探针仍残留 14 个 fd。

下一阶段应区分显示级、上下文级与线程级保留，验证驱动支持的完整清理路径；若无法在同一进程内释放，应评估避免无外屏时初始化 NVIDIA EGL，以及需要 NVIDIA 输出时如何重新接管。避免初始化只能覆盖部分场景，不能冒充已实现有外屏使用后的动态卸载。
当前不需要新一轮桌面注销试验，尚未修改 Mutter 补丁或 GPU 管理服务。

## 复现与证据

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/gbm_context_probe.py --pci 0000:03:00.0
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/gbm_context_probe.py --pci 0000:03:00.0 --with-context
```

不加 sudo。工具会初始化目标显卡并可能唤醒它；只改变探针进程自己的资源。清理失败时记录错误，不把该次实验当作成功释放。
机器可读汇总、源报告名和 SHA256：[证据](evidence/20261107-gbm-context-probe.json)。完整 fd 快照留在用户的 poc-reports 状态目录。
验证包括两个真实 GPU 子进程实验及 Python 语法检查，不代表真实 GNOME 动态释放功能通过测试。

## 重复周期与线程退出对照

新增 `tools/gbm_lifetime_series.py`。两个独立进程分别执行三轮完整探测：一个始终使用同一线程，另一个每轮新建工作线程，执行清理并等待该线程退出后才扫描 fd。
两组进程并行运行，但每个记录均来自其自己的 `/proc/self/fd`；不据此比较性能或跨进程的驱动内存用量。

| 模式 | 第一轮结束 | 第二轮结束 | 第三轮结束 |
| --- | ---: | ---: | ---: |
| 同一线程，显式 release-thread | 14 | 14 | 14 |
| 每轮新线程，release-thread 后退出 | 14 | 14 | 14 |

每轮所有清理调用成功，两个子进程正常退出；剩余节点分布均为 4 个 nvidiactl、7 个 nvidia0、2 个 renderD129、1 个 nvidia-modeset。
本次短测没有发现 fd 逐轮增长，线程退出也没有进一步关闭它们。结果更支持进程内缓存或全局引用保留这一解释，不能据三轮数据证明所有资源都不泄漏，也不能确定驱动内部的对象所有权。

复现命令：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/gbm_lifetime_series.py --pci 0000:03:00.0 --cycles 3
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/gbm_lifetime_series.py --pci 0000:03:00.0 --cycles 3 --fresh-thread
```

证据：[线程与周期汇总](evidence/20261107-gbm-thread-series.json)。每轮保留完整快照，脚本最长等待整个子进程 25 秒；不做强制关闭目标进程 fd 或动态库卸载。

## 官方接口核查与当前结论

[EGL 1.5 规范](https://registry.khronos.org/EGL/specs/eglspec.1.5.pdf) §3.12 把 release-thread 限定在线程状态；§3.2 描述显示的终止与可重新初始化，§3.3 脚注指出 EGLDisplay 句柄创建后在应用生命周期内保持有效。
这些规定没有提供“返回成功就关闭全部 Linux GPU fd”的承诺，也不能据句柄有效就反过来认定驱动必须保留设备 fd。我们检查的这些标准清理接口尚未给出满足本项目目标的完整释放入口。

[NVIDIA 595.99.02 eGPU 文档](https://download.nvidia.com/XFree86/Linux-x86_64/595.99.02/README/egpu.html) 不保证使用中的 eGPU 热移除稳定性。其桌面配置说明主要针对 X11，不能把它当成所有 Wayland 热移除路径的定论。

目前不应继续在 GNOME 中堆叠 terminate 或 release-thread。后续研发应分成两条：

1. **向驱动维护方询问进程内完整设备释放语义。** 已备好本地问题草稿与独立复现，没有创建外部 issue 或发送消息。
2. **减少不必要的进程内 NVIDIA 初始化。** 在无外屏时延迟初始化可避免首次建立引用，需同时设计外屏接入后的初始化；它不能解决已经使用 NVIDIA 输出后再次拔卡。单纯设置现有 multi-GPU copy-mode 也不能跳过前面的 RenderDevice 初始化。

若最终需要把 NVIDIA 访问移到独立进程，工作量将涉及 KMS/显示输出协调、跨进程帧传递和恢复。进程退出也不自动证明显卡释放：其他进程持有的 NVIDIA dma-buf、导出同步对象及客户端引用都必须排空。该架构尚未实现，不作为当前软件能力宣传。

后续已验证 AMD 硬件渲染与不建立 NVIDIA EGL 的 DRM/KMS 查询、私有 dumb buffer 分配共存，且相应 NVIDIA fd 可完整关闭。详见 [DRM-only 路径可行性](2026-11-07-drm-only-feasibility.md)；尚未驱动真实外屏。
