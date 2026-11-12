# 2026-11-06 实机引用日志观察

用户成功登录修复后的实验 GNOME，并执行 `control.py inspect`。PoC 总线进程与当前 GNOME Shell 服务主进程一致。AMD `/dev/dri/card1` 为 primary，存在一个 view，无 lingering onscreen；单次试用覆盖已撤销。

## 已观察到的释放过程

本次 NVIDIA 副卡没有外屏。启动日志依次出现：

```text
normal-cache-decision path=/dev/dri/card0 retained=0
gpu-data-free path=/dev/dri/card0
render-device-dispose path=/dev/dri/card0
egl-terminate-begin
egl-terminate-returned
gbm-destroy-begin
gbm-destroy-returned
render-device-finalize path=/dev/dri/card0
device-release path=/dev/dri/card0 fd=12 refs=0
device-close-returned path=/dev/dri/card0 fd=12
```

只读接口返回 `cache_present=false`。这证明在**本次登录前已连接副卡的场景**中，正常回收逻辑已经运行，受插桩跟踪的 RenderDevice 已 finalize，DRM 主节点 DeviceFile 引用归零关闭。不能把 `Inspect` 的 `render_device_finalized=false` 当成资源未销毁：该字段表示本次请求的回收观察，而 Inspect 没有执行回收。

## 仍然存在的占用

同一 GNOME 进程仍持有 `/dev/dri/renderD129`、`/dev/nvidia0`、`/dev/nvidiactl` 和 `/dev/nvidia-modeset`。sysfs 确认 renderD129 对应目标 PCI `0000:03:00.0`。

进一步只读审计记录到上述节点共 21 个 fd。进程映射中可见 NVIDIA EGL、GBM、allocator、eglcore 等库，但**库映射本身不能确定哪个调用或对象持有这些 fd，也不能据此认定驱动泄漏**。

没有执行 `Reclaim`，没有改变当前桌面、关闭 fd、卸载设备或拔线。缓存已不存在，重复回收此缓存不能解决现有占用。

## 下一步定位范围

本次证据排除了“此场景仅因未触发 gpu_datas 清理”这一解释；尚未验证登录后热插入等其他场景。
下一阶段应记录每次 NVIDIA/EGL 初始化前后的 fd 差异，以及 EGLDisplay/GBM 对象建立、销毁和复用关系，定位缓存之外的全局对象、其他 EGLDisplay 或用户态驱动内部保留。不能直接强关 GNOME 的 fd，也不能在当前进程强行卸载 NVIDIA 动态库。

原始材料保存在当前用户的 `~/.local/state/usb4-gpu-manager/poc-reports/`：

- `20261106-003110-88689172.json`：用户执行的 Inspect。
- `20261106-residual-reference-audit.json`：后续只读 fd、库映射与插桩事件审计。

实际 GPU 引用全部释放、原窗口持续绘制及安全移除仍未验证。

## 独立 NVIDIA EGL 最小复现

进一步使用 `tools/egl_reference_probe.py` 在普通用户的独立子进程中复现。脚本只加载系统 NVIDIA EGL vendor，按 PCI 匹配设备，不建立 context、surface、窗口或模式设置，不操作 GNOME 的对象或 fd。子进程最长等待 15 秒，完整结果写入用户报告目录。

本机驱动版本为 595.99.02，结果如下：

| 阶段 | 子进程内相关 fd 数量 |
| --- | --- |
| 加载 libEGL 前 / 后 | 0 / 0 |
| `eglQueryDevicesEXT` 后 | 5 |
| 获取目标 EGLDisplay 后 | 6 |
| `eglInitialize` 成功后 | 8 |
| `eglTerminate` 成功后 | 8 |
| `eglReleaseThread` 成功后 | 8 |

最后仍包含目标 `renderD129`、`nvidia0` 和共享 `nvidiactl`。子进程正常退出，父进程没有让其常驻；实验后真实 GNOME 的 PID 不变且服务仍 active。

这提供了**不依赖 Mutter 缓存的独立复现**：NVIDIA EGL 的设备枚举及显示初始化可以留下进程存活期间的设备 fd，单次成功 terminate/release-thread 不保证关闭它们。不能单凭此结果认定驱动泄漏，也不能将子进程中 8 个 fd 与 GNOME 的 21 个 fd 逐一对应。

本轮先建立独立复现，尚未增加新的 Mutter C 插桩。后续应对照这条 EGL 调用链，定位 GNOME 内部初始化来源、其他 EGLDisplay 与全局资源生命周期，并研究是否存在受支持的完整释放路径。不要尝试在运行中的 GNOME 强制 `close` 或 `dlclose` NVIDIA 库。

复现命令（会在独立子进程初始化 NVIDIA EGL，可能唤醒显卡）：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/tools/egl_reference_probe.py --pci 0000:03:00.0
```

机器可读结果：[独立 EGL 生命周期证据](evidence/20261106-nvidia-egl-reference-probe.json)。这是资源生命周期探针，不是安全拔卡检查；无需为重复观察而再次注销当前会话。

## 下一轮插桩准备

现已完成 MetaEgl/GBM 生命周期边界与 fd 快照插桩，新增 RenderDevice、EGLDisplay、GBM 对象关联事件。
新版在独立 `mutter-poc-trace-v2` 前缀编译，未覆盖正在使用的旧实验库；开启追踪的隔离软件 GNOME 成功启动并正常退出，库身份检查通过。79 项 Python 回归及 8 项 ASan/UBSan C 替身测试通过。
当前真实 GNOME 仍是旧实验进程。只读 Inspect 的新日志采集器已经在该进程上运行，但旧进程不能产生新版的启动期事件。
下一次采集需要保存工作后手动进行新版单次登录试用；操作步骤和关联局限见 [PoC README](../../experimental/mutter-poc/README.md)。

## 新版实机采集结果（00:51）

用户提供 `20261106-005123-be17cab8.json`。新版 GNOME 正常运行，PoC 进程与服务主进程一致，AMD primary、一个 view、无 lingering，试用 drop-in 已撤销。
报告含 323 条 PoC 事件，journal 未提示达到 4000 行上限。16 组 fd 快照逐组核对，记录数量与 matched 一致，未见交错分组或扫描截断。

以下只计算 `/dev/nvidia*` 及目标 `renderD129`，不包括 Mutter 已打开的 DRM 主节点 card0，也不包括 AMD 节点：

| 实机边界 | NVIDIA/共享 NVIDIA fd 数 |
| --- | --- |
| NVIDIA GBM 创建前 / 后 | 0 / 0 |
| NVIDIA GBM 平台 EGLDisplay 获取后 | 6 |
| 对该 EGLDisplay initialize 成功后 | 8 |
| 接下来 AMD GBM 创建前 | 20 |
| NVIDIA EGL terminate 成功前 / 后 | 20 / 20 |
| NVIDIA GBM destroy 前 / 后 | 20 / 20 |

对象关联日志明确把第一个 GBM 和 EGLDisplay 对应到 `/dev/dri/card0`，第二组对应 AMD card1。NVIDIA 的 RenderDevice 随后 finalize，受跟踪 card0 fd 引用归零关闭；Inspect 时缓存已不存在，但 NVIDIA 设备节点仍被持有。

本次没有 `egl-call operation=query-devices` 事件。因此不能把此前独立 EGLDevice 探针的枚举调用当成 GNOME 本次首次占用的具体入口；本次观测到的首次新增位于 GBM 平台 `eglGetPlatformDisplayEXT` 的前后边界。该结论仍受非原子 fd 快照的并发限制。

尚有一个明确缺口：从 NVIDIA initialize 返回，到 AMD GBM 创建前，又增加了 12 个 NVIDIA fd。源码中该区间包含 `detect_hardware_rendering()` 的临时 EGL context 创建、make-current、GL_RENDERER 查询及 context 销毁，也包含后端后续初始化。这些调用还没有独立边界日志，不能把 12 个 fd 全部归因于某一个调用。

下一步应先在独立子进程复现 GBM 平台及上下文探测链路，再决定是否需要新一轮合成器插桩。当前仍未找到保留原 GNOME 会话时完整释放 NVIDIA 访问的实现；不执行 Reclaim 或设备移除。

地址匿名化的快照汇总与原报告 SHA256：[新版启动期证据](evidence/20261106-trace-v2-startup.json)。

2026-11-07 已完成独立 GBM/context 对照实验，复现 create-context 增加 6 个、make-current 再增加 6 个 NVIDIA fd；release-thread 仅关闭其中部分，仍不能实现安全释放。详见 [后续实验报告](2026-11-07-gbm-context-lifetime.md)。
