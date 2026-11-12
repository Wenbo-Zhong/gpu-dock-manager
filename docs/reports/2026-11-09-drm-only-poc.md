# 2026-11-09：DRM-only／CPU-copy Mutter 实验版

独立实验构建由 AMD 合成桌面，指定外接 NVIDIA 的输出走 primary CPU-copy 和 DRM dumb buffers。无外屏登录时 GNOME 的目标设备 fd 扫描为空。v3 已在真实双屏确认跨屏光标和内屏画面正常。

## 外屏光标修复

v1 在 NVIDIA DP-12 上分配了 2560×1440 输出。用户能把窗口拖到外屏，但跨屏后光标消失。Inspect 显示两块屏幕处于同一 GNOME 会话中，AMD 仍为主卡，GNOME 持有的目标设备 fd 只有 card0。

Mutter 的原生光标逻辑把所有视图的硬件光标状态合并成一个布尔值。任一视图仍使用硬件光标，整个软件光标层就会被隐藏。v1 只为 NVIDIA 禁用硬件光标，因此 AMD 内屏的硬件光标状态也可能隐藏外屏需要的软件光标。

v2 将同一次 DRM-only 试用中的所有 GPU 统一切到软件光标，包括后续加入的 GPU。实机报告 `20261109-213427-74426ff2.json` 对应 PID 3556086，两个视图，AMD 为主卡，GNOME 在目标及共享 NVIDIA 节点中仅持有 card0。用户确认跨屏光标正常，同时报告 AMD 内屏局部花屏。现有日志无法定位花屏原因。

v3 使用 Mutter 已有的视图级光标层抑制机制。硬件光标可用且成功建立的视图跳过软件层，其余视图仍可绘制软件光标。AMD 恢复正常的硬件光标选择和回退，NVIDIA 目标仍禁止使用硬件光标。

v3 实机报告 `20261109-214722-b18a4a82.json` 对应 GNOME PID 3637852，已核对进程加载的是 v3 私有库。用户确认双屏和光标正常。日志记录 AMD card1 硬件光标可用、NVIDIA card0 使用软件光标以及 DP-12 的 2560×1440 输出缓冲分配。两路输出启用，完整 fd 扫描只找到 card0 这一目标节点。

## v3 同一会话停用外屏

报告 `20261109-215043-f3f10f55.json` 与双屏报告的 PID 和进程启动时间均一致。DP-12 仍物理连接，输出已停用。AMD 内屏仍启用，GNOME 视图从两个降为一个，lingering onscreen 为零。

日志记录 NVIDIA KMS hold 降为零、普通缓存回收销毁目标 GBM 和 RenderDevice、DeviceFile 引用归零并关闭 card0。Inspect 的完整目标 fd 扫描为空，随后独立核对仅见 AMD DRM 节点。整个过程由停用输出触发，未调用 Reclaim。

管理服务仍判定不能安全移除。完整扫描识别出 Xwayland 持有 card0，Clash Verge 的 WebKit 子进程和 Ptyxis 持有 NVIDIA render 或驱动节点。软件停用输出和合成器释放已得到验证，物理移除仍需处理其他进程占用。

## 其他进程占用与 v4

Xwayland 24.1.10 的 AMD 渲染节点是 renderD128。另一个 NVIDIA card0 fd 来自 Wayland 的 `wp_drm_lease_device_v1.drm_fd`。对照实验在不改动当前桌面的情况下启动了独立 Xwayland，协议日志与 `recvmsg(SCM_RIGHTS)` 记录显示，Mutter 传给它 card0 和 card1 两个 fd。即使租赁设备未发布可租用的接口，绑定时也会收到 fd。只让独立 Xwayland 加载 Mesa EGL 后仍复现。Xwayland 源码在收到租赁设备撤回并完成释放，或关闭屏幕时关闭此 fd。

v4 将命中已审计 PCI 策略的外接 NVIDIA 设备排除在 DRM 租赁设备列表之外，连接器处理复用同一列表。该卡的正常显示仍走 v3 的 KMS/CPU-copy 路径。依赖 DRM 租赁的程序（如部分 VR 程序）在本次试用中无法租用该卡。隔离环境仅暴露两张卡的 render 节点，Xwayland 在 v3 绑定两个租赁设备，在 v4 绑定一个，两次均启动成功。

v4 实机报告 `20261109-225537-9bf74ee4.json` 核对了 NVIDIA 输出分配和停用后的回收。GNOME 日志显示跳过 card0 租赁设备，外屏停用后没有 NVIDIA 设备句柄。正在运行的 Xwayland 只持有 AMD 的 card1 和 renderD128。管理服务完整扫描只发现 Clash Verge 的 WebKit 子进程持有 NVIDIA 节点，物理移除仍待验证。

GTK 4 的独立 Wayland 探针在默认 Vulkan 渲染器下复现了 Ptyxis 的 NVIDIA 节点组合。将 Vulkan ICD 限定为 Radeon、EGL vendor 限定为 Mesa 后仍使用 Vulkan，fd 只剩 AMD renderD128。选择 Cairo 的对照没有任何 DRM fd。

Clash Verge 的在用 WebKit 子进程已带 `WEBKIT_DISABLE_DMABUF_RENDERER=1`。使用同一 WebKitGTK 版本的独立离屏 WebView 复现了持有 NVIDIA 句柄。在保留该环境变量的同时限定 Mesa EGL 后，页面仍能加载，WebKit 子进程只持有 AMD renderD128。两项应用测试验证的是同库探针。仍需用实际应用的全新实例验证，进程级环境变量无法迁移现有实例。

## v4 安全移除及无外屏重接

用户确认安全移除成功，管理服务随后确认目标 PCI 设备不再可见。重接后相同 PCI 地址的显卡由 card0 变为 card2。报告 `20261109-231155-cc5c3041.json` 确认 GNOME 仍是原进程，AMD 内屏保有唯一视图，card2 的 RenderDevice 已销毁但 fd 99 仍打开。管理服务同时扫描到 systemd 和 systemd-logind 持有 card2。根据 Mutter 的设备池和 KMS 初始化代码，首轮模式设置前保留的 KMS 引用与该现象吻合，尚未通过模式设置后的对照确认。

同一会话随后出现 card2 的 2560×1440 输出分配，停用后报告 `20261109-232109-22cf45b0.json` 显示相同 PID 和启动时间、AMD 唯一视图、目标缓存已清理、完整目标 fd 扫描为空。系统扫描中的 systemd、systemd-logind 和 GNOME 三条占用同时消失。剩余 Ptyxis 持有目标 renderD130 等节点，ROG Control Center 只持有共享的 `/dev/nvidia-uvm`。单凭这个共享节点无法归因到某张卡，移除检查仍保守拦截。

管理器已修复 PCI 设备重新出现时继续显示上一次“可以拔线”的状态提示。

## 第一轮实机结果

用户执行 Inspect，报告 `20261109-001317-af3c7158.json`。PoC PID 与当前 GNOME 服务主进程一致，AMD card1 为 primary，存在一个 view，无 lingering onscreen；Inspect 完整扫描的目标/共享 NVIDIA 节点列表为空。
同一 GNOME 进程实例又进行了两次间隔一秒的只读 fd 核对：均未见 card0、renderD129 或 `/dev/nvidia*`。这是时点观察，不是所有未来时刻或所有缓冲资源的保证。

78 条插桩事件没有达到采集上限，显示：

- card0 命中 `drm-only-device`、`drm-only-cpu-copy` 和 `drm-only-software-cursor`。
- 唯一记录的 RenderDevice EGLDisplay 对应 AMD card1，没有记录目标 NVIDIA RenderDevice EGLDisplay。
- card0 的 KMS hold 降为零，随后正常缓存回收销毁 GBM/RenderDevice。
- 受跟踪 DeviceFile 引用降为零并关闭 card0。
- 单次试用已消耗，服务 drop-in 为空，当前实验会话继续运行。

结论：本次**无外屏场景**中，原先观察到的 GNOME NVIDIA 设备 fd 残留已消失；不是主动 Reclaim 的结果。
没有检查并释放所有其他应用的 GPU 资源，没有执行系统移除或拔线。设备 fd 消失也不单独证明全部 dma-buf、同步对象或内核引用消失。
下一轮应在保留此会话的情况下手动连接一个外屏，验证显示和重绘，再停用输出并观察回收；不要提前注销丢失本次实验状态。

机器可读记录：[实机无外屏证据](evidence/20261109-drm-only-live-no-external.json)。

## 改动范围

- `MetaRenderDeviceGbm` 按 PCI 身份和 sysfs 属性标记指定目标，跳过其 EGLDisplay 创建；后端直接接受该 GBM 对象，阻止 EGLStream 回退。
- 目标 secondary GPU 直接选择 PRIMARY＋强制 CPU-copy，跳过 secondary EGL context 初始化；不修改 AMD 的正常 GPU 路径。
- 目标的 GBM dma-buf 分配、导入、modifier 查询返回不支持；对外的 GBM device 获取不暴露此目标，避免 Wayland 直接扫描输出和光标导入。
- 目标输出拒绝直接扫描输出，强制合成。目标硬件光标禁用，交给软件合成，避免独立光标缓冲在停用外屏后保留设备引用。
- 使用现有 DRM dumb buffer 分配与 CPU-copy 输出路径，输出缓冲建立时记录 `drm-only-output-allocated` 事件。没有添加 PCI 删除、进程终止或强关 fd。

运行时要求 opt-in、有效目标 PCI、NVIDIA vendor、`boot_vga=0`、`removable`；选中的主卡必须是 AMD。对象上的策略标记避免每帧重复读取 sysfs。
这只限制合成器的访问，不管理 CUDA/其他客户端；这些进程仍可能占用设备。

## 构建与验证

独立目录：`~/.cache/usb4-gpu-manager/mutter-poc-drm-only-v1/`。未覆盖当前会话使用的 trace-v2 库，也没有替换系统软件包。
构建命令在现有干净基线和已准备的构建依赖环境下为：

```bash
/usr/bin/python3 experimental/mutter-poc/build.py --source /home/user/.cache/usb4-gpu-manager/mutter-poc-audited-base --work /home/user/.cache/usb4-gpu-manager/mutter-poc-drm-only-v1 --drm-only --jobs 8
```

已完成真实源码编译、私有安装、原生/GI 库一致性、完整隔离 GNOME JS 启动、81 项 Python 检查及 9 项 ASan/UBSan C 替身测试。替身测试验证设备策略与拒绝条件，不能代替实际输出测试。

进一步在独立无头 GNOME 中仅暴露 AMD 和 NVIDIA 的 render 节点，隐藏所有 DRM 主节点、宿主总线和用户 HOME。两家 EGL vendor 可用，日志确认 NVIDIA DRM-only 与 CPU-copy 分支执行，primary 为 AMD，Shell JS 和显示配置接口正常。
此测试没有打开 `/dev/nvidia*` 用户态设备节点，但保留一个由 KMS 管理的目标 render fd，最终退出时释放；不能据此宣称活跃会话中目标已无引用。无头模式没有实际 CRTC/外屏，未验证 CPU-copy 的物理输出或软件光标路径。

机器可读记录：[构建与测试证据](evidence/20261109-drm-only-build.json)。源码补丁、构建清单、库和日志均可在本机检查。

## 第一轮：只验证无外屏登录

先保存工作，使用 AMD 内屏；保持 eGPU 连接但暂不接外屏、不运行 GPU 计算。普通用户执行：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/session.py enable --manifest /home/user/.cache/usb4-gpu-manager/mutter-poc-drm-only-v4/manifest.json --drm-only
```

两处选择必须匹配：清单标记 DRM-only，命令显式指定 `--drm-only`。它仍只准备下一次登录；启动前撤销试用覆盖。启动器核对当前系统版本、补丁/库哈希、目标 PCI 和可移除属性；检查或撤销失败时使用系统版本。

保存工作后正常注销，重新登录 Wayland，再运行：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/control.py inspect
```

先反馈报告路径，保留该会话。预期 AMD primary、内屏视图存在、日志有 `drm-only-device` 和 `drm-only-cpu-copy`，没有目标 NVIDIA EGLDisplay／secondary context 初始化。缓存可以已经被正常清理；不要求它存在。
重点核对 `/dev/nvidia*` 是否消失，以及目标 DRM fd 是否仍被 KMS/其他对象持有；不是只看一个布尔值。当前不要 Reclaim、移除 GPU 或拔线。

## 后续真实外屏步骤

只有第一轮证据确认后，再手动接一个外屏，以基础 SDR 模式开始检查画面、鼠标、重绘，并执行 Inspect。该只读动作现在允许已知的 connected 状态；Reclaim 仍拒绝接屏目标。
随后在当前 v3 会话保留视频线和 USB4 线，使用管理器的“停用外屏”。再次 Inspect，比较进程启动时间、视图数量、目标 fd 及缓存回收事件，并确认已有窗口留在 AMD 桌面。性能、色彩/HDR/VRR、睡眠恢复、客户端 dma-buf 引用、最终移除与重接均待验证。
本实验主动牺牲目标 GPU 的直接扫描输出和硬件光标，不应当作生产驱动更新。

## 恢复

仍可在同一用户的 TTY 中执行：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/session.py disable
```

试用启动前自动撤销覆盖，下一次登录应恢复系统版本；它不保证已经失败的那一次登录无缝重试。没有改动现有开机/恢复服务或 NVIDIA 内核参数。
