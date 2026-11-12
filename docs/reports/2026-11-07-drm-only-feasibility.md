# 2026-11-07：AMD 硬件渲染与 NVIDIA DRM-only 路径可行性

## 为什么做这一轮

此前已发现 NVIDIA EGL 初始化留下进程内设备 fd，普通资源清理和线程退出不能完整关闭它们。本轮检验能否在 AMD 渲染时避免初始化 NVIDIA EGL，同时保留发现 NVIDIA 输出设备和分配输出缓冲的基础能力。

新增 `tools/deferred_nvidia_probe.py`。所有操作在普通用户的独立子进程中进行，GLVND 使用系统默认 vendor 配置，未屏蔽 NVIDIA vendor。没有模式设置、窗口、EGL surface 或对真实屏幕的提交。

## 软件回退的校正

最初使用 AMD card1 主节点的三组探针返回 llvmpipe，已排除出硬件结论。该现象不能推断当前 GNOME 正在软件渲染；独立客户端的主节点访问与合成器取得的设备访问不同。
改用 AMD renderD128 后，GL_RENDERER 明确为 **AMD Radeon 890M Graphics（radeonsi、strix1、ACO）**。下述五组均使用这个硬件路径。

## 实测结果

NVIDIA 目标为 card0/renderD129，AMD 为 renderD128。表中的“保留 fd”指子进程关闭 NVIDIA 自有资源后、AMD 上下文仍有效时的 NVIDIA 设备 fd；AMD 资源最后单独清理。

| 独立子进程内的操作 | NVIDIA 保留 fd | AMD 上下文检查 |
| --- | ---: | --- |
| 只初始化 AMD | 0 | 同一 context，硬件 renderer 不变 |
| AMD + NVIDIA DRM 资源及连接器查询 | 0 | 同上 |
| AMD + 临时 NVIDIA GBM 设备，随后销毁 | 0 | 同上 |
| AMD + NVIDIA 私有 dumb buffer 分配、映射、写入、销毁 | 0 | 同上 |
| AMD + 完整 NVIDIA EGL/context 初始化、清理，再恢复 AMD | 14 | 显式恢复成功，硬件 renderer 不变 |

DRM 查询使用 `drmModeGetResources` 和 `drmModeGetConnectorCurrent`，成功读取四个连接器；后者只读取当前缓存信息，不强制重新探测屏幕。
私有 dumb buffer 为 64×64、32bpp、pitch=256、size=16384 字节。成功 mmap 并写入、读回 4 字节，随后解除映射并销毁缓冲；没有 framebuffer 注册、CRTC/plane 改动或显示提交。映射时 Python mmap 持有的附加 card fd 也随映射关闭。

五个子进程正常退出。真实 GNOME 的进程与服务状态保持不变，没有变更其环境、动态库、显示器或 GPU 管理服务。

## 已得到与尚未得到的结论

**已有直接证据：** 在本机，AMD 硬件上下文可以和短期的 NVIDIA DRM/KMS 查询、GBM 设备、CPU 可写 dumb buffer 共存，且这些 NVIDIA 访问可以关闭干净。NVIDIA EGL 初始化不是完成这些基础操作的必需步骤。

**尚不能宣称：** 外屏已经能以 DRM-only 模式工作；实际输出需要 framebuffer、模式设置、page flip、颜色处理及同步等生命周期验证。也尚未证明这种路径的性能、热插拔、睡眠恢复和最终安全移除。

本轮没有调用 NVML/CUDA。避免桌面初始化 NVIDIA EGL 不等于已验证 CUDA；计算进程仍可能独立占用显卡，最终移除必须继续检查所有持有者。

## 合成器 PoC 的具体切入点

源码仍为本机 Ubuntu Mutter 50.1 基线：

1. `meta-backend-native.c:add_drm_device()` 当前先 `create_render_device()` 再建立 KMS 设备。NVIDIA RenderDevice 初始化会提前走进 EGL。需要把受限目标的 KMS 元数据和 NVIDIA EGL 初始化分离。
2. `create_render_device()` 对非硬件 GBM 路径还会尝试 EGLStream；只跳过第一次 EGL 初始化不够，必须防止回退再次建立 NVIDIA EGL 引用。
3. `meta-renderer-native.c:create_renderer_gpu_data_gbm()` 无条件尝试 `init_secondary_gpu_data()`；也需要识别 DRM-only 目标，避免建立 NVIDIA secondary EGL context。
4. `meta-onscreen-native.c` 已有 primary-copy、dumb buffer 与 CPU-copy 的路径，可作为输出 PoC 的基础。但现有 copy-mode 环境变量不能阻止之前的 RenderDevice 初始化。
5. 还必须审查直接扫描输出、GBM import/分配、dma-buf 导出和上下文恢复路径，避免输出阶段绕回 NVIDIA 用户态分配器或 EGL。一次“未调用 EGL”的标记不能代替 fd 与缓冲引用审计。

建议下一阶段制作**明确 opt-in、仅限 AMD 内屏为主卡和指定外接 NVIDIA 的 DRM-only/CPU-copy PoC**。先验证无外屏登录时 GNOME 不建立 NVIDIA 用户态引用，再验证真实外屏输出与停用后的回收。当前尚未修改或构建这部分 Mutter 行为补丁。

CPU-copy 可能有带宽、功耗与延迟代价；这里没有测量这些指标，不把它宣传成正常 GPU 合成路径的等价替代。CUDA 及其他客户端不应被该桌面策略强制结束。

## 复现与证据

示例（普通用户，不加 sudo，GPU 会被探测或初始化）：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/deferred_nvidia_probe.py --amd-pci 0000:66:00.0 --nvidia-pci 0000:03:00.0 --case amd-only
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/deferred_nvidia_probe.py --amd-pci 0000:66:00.0 --nvidia-pci 0000:03:00.0 --case amd-plus-nvidia-kms
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/deferred_nvidia_probe.py --amd-pci 0000:66:00.0 --nvidia-pci 0000:03:00.0 --case amd-plus-nvidia-dumb-buffer
```

另支持 `amd-plus-nvidia-gbm`、`amd-plus-nvidia-egl`。整组子进程期限为 20 秒。
验证为五组实际硬件对照与 Python 语法检查；地址无关汇总、原始报告名及 SHA256 见 [证据](evidence/20261107-deferred-nvidia-feasibility.json)。原始失败的软件回退对照名也保存在证据中，避免与硬件结果混淆。
