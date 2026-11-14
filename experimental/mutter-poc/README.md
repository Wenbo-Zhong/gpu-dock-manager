# Mutter 50.1 副卡引用日志与缓存回收 PoC

目标：验证 AMD 主卡继续承载同一个 GNOME 桌面时，是否可以清理 Mutter 自身的闲置 NVIDIA 副卡渲染资源。**这不是 GPU 弹出功能，不执行 PCI 删除、模块卸载、进程终止或 TTY 切换。**

**2026-11-05：初版导致图形登录失败，用户在 TTY 执行 `session.py disable` 后恢复。**
原因是私有 Clutter/Cogl/Mtk 与系统 GI 加载的另一份库混用，重复注册 `ClutterActor`；编译、`ldd` 和 `--version` 检查漏掉了运行时类型加载。
诊断、证据和修复见 [登录故障记录](../../docs/reports/2026-11-05-poc-login-failure.md)。

现已统一使用系统同版本的 Clutter/Cogl/Mtk，只保留修改过的私有 Mutter 主库。
新增原生/GI 符号地址及实际映射检查，并在无真实显卡、无宿主系统总线的隔离环境中启动 GNOME JS、查询显示配置，确认启动后持续存活。
**2026-11-06：修复后的实验 Wayland 会话已成功登录，单次试用覆盖已自动撤销。**
实机日志显示正常回收已销毁副卡渲染缓存及受跟踪的 RenderDevice，但 GNOME 仍持有 NVIDIA 句柄；未执行主动回收或设备移除。
详见 [实机引用观察](../../docs/reports/2026-11-06-poc-reference-observations.md)。缓存已不存在时不要重复执行 `reclaim`。

## 当前 DRM-only 单次试用

当前 DRM-only／CPU-copy 实验版位于独立目录 `~/.cache/usb4-gpu-manager/mutter-poc-drm-only-v4/`。
v2 实机恢复了外屏光标，同次试用出现内屏局部花屏。v3 按视图决定是否绘制软件光标，AMD 内屏恢复原有硬件光标路径，NVIDIA 外屏继续使用软件光标。
v3 实机已在同一个桌面会话停用外屏并释放 GNOME 自身占用。v4 排除目标 NVIDIA 的 DRM 租赁设备，以免 Xwayland 从 Mutter 接收并持有 card fd。
构建、设备限制、测试边界和分阶段步骤见 [DRM-only 试验说明](../../docs/reports/2026-11-09-drm-only-poc.md)。未覆盖当前会话的 trace-v2 库；旧清单与新补丁校验和不同，不能混用。

在 GNOME 桌面的终端执行，**全部以普通用户运行，不加 sudo**：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/session.py enable --manifest /home/user/.cache/usb4-gpu-manager/mutter-poc-drm-only-v4/manifest.json --drm-only
```

启用前必须通过对应构建的隔离 GNOME 启动验证，并现场复查原生/GI 库一致性；旧构建被拒绝。
它只创建当前 GNOME Shell 用户服务的一个 drop-in，并刷新用户服务定义。当前 Shell 不被停止或重启。
先保存工作，然后通过系统菜单正常注销、重新登录一次，才会加载私有 Mutter。
启动实验 GNOME 前，启动器先撤销试用 drop-in 并刷新用户服务定义，之后才启动私有 Mutter；若撤销失败，直接使用系统版本。
因此只试用一次：实验桌面退出后，下次登录使用系统版本。若实验启动失败，再次登录也应恢复系统版本；`disable` 仍作为 TTY 恢复入口。

已验证本机桌面后，可在同一条 `enable` 命令追加 `--persistent`。每次登录前仍复核版本、库和补丁哈希、GI 以及目标 PCI。正常登录保留配置。若 Shell 未向 systemd 报告就绪或用户服务异常退出，后续登录改用系统 Mutter，重新执行 `enable --persistent` 才重新尝试。换包造成的校验失败只影响该次登录。已登记的 eGPU 不在场时仍能在 AMD 内屏启动，热插拔时由补丁重新验证设备身份。使用 `session.py status` 查看最近一次启动记录。
这次初始登录需要注销：已经加载系统库的现有进程不能安全地换入本补丁。之后的回收实验本身不注销，目标是保留新会话里已经打开的窗口。

保持 USB4 连接；先限定为 eGPU 无外屏、AMD 内屏开启。不要进行 GPU 计算；尽量保留一个明确由 AMD/CPU 绘制的窗口作观察。
登录后先运行只读检查：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/control.py inspect
```

查看生成的 JSON。`initial.primary_driver` 应为 `amdgpu`；无外屏且内屏正常时 `initial.eligible` 应为 true。缓存可能已经被正常清理，不要求 `initial.cache_present` 为 true。
新版 Inspect 会附带当前进程的启动期 PoC journal 事件（最多读取最近 4000 行，报告可能截断情况），用于分析缓存已消失但 fd 仍存在的情况。
若缓存本来就不存在，直接分析日志和残余句柄，不重复回收。
**当前 DRM-only 首轮只做 Inspect，不执行回收。** 以下为原有缓存回收研究接口，仅在另外确认条件后使用：

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/control.py reclaim
```

客户端确认 PoC 总线与当前 `org.gnome.Shell` 属于同一进程，再向固定的唯一总线 owner 发请求。
它会记录原 PID / 启动时间、回收前后相关设备 fd、AMD 主卡和 view 数量，在约 3 秒内多次复查，并采集该 GNOME 进程的 PoC journal 事件。
结果保存到 `~/.local/state/usb4-gpu-manager/poc-reports/`（或 XDG_STATE_HOME 对应目录），权限 0600。
请将报告路径反馈，**不要因为句柄消失就拔线**。

必须区分几种结果：

| 结果 | 意义 |
| --- | --- |
| `cache-reclaimed-not-a-gpu-eject` | 删除了指定副卡的渲染缓存，仍不是安全移除完成 |
| `render_device_finalized=true` | 弱引用确认对应 RenderDevice 已销毁，不等于驱动所有引用已释放 |
| 缓存删除但 NVIDIA fd 仍存在 | 继续查 EGL/GBM、DeviceFile、用户态驱动引用 |
| 缓存过后又出现 | 存在重新初始化路径，尚未实现逻辑离线 |
| `cache-already-absent-no-mutation` | 未发生新的回收，不能算本 PoC 解决了占用 |
| 原进程消失、重建、主卡变化、上下文恢复失败 | 验证失败，不能宣称保留桌面成功 |

原 GNOME PID、AMD view 持续存在只能提供部分证据，还要人工确认已有窗口内容、输入和重绘正常。
Xwayland、Ptyxis、CUDA 等其他进程可能继续占用，PoC 不会处理或终止它们。

## 恢复系统版本

```bash
/usr/bin/python3 -I /home/user/gpu-dock-manager/experimental/mutter-poc/session.py disable
```

删除本 PoC 的用户 drop-in，下次正常注销登录使用系统 Mutter；不会强制结束当前会话。
若图形登录失败，切到 TTY 登录同一用户后执行上述命令，再正常重启或重新登录。
没有替换 `/usr/lib` 或系统软件包。系统 Mutter 版本变化、构建清单、GI 一致性或补丁校验失败时，启动器自动退回系统库。
这些启动前检查不能捕获已经开始运行的 GNOME 内部故障；单次试用保证后续登录撤回覆盖，不承诺原次登录无缝恢复。

材料风险：销毁 EGL/GBM 必须在合成器进程里进行，无法像外部探测一样通过杀掉子进程保证解阻塞；NVIDIA 用户态调用若卡住，仍可能拖住实验桌面。初次实验前保存工作。
客户端超时只意味着没有及时收到回复，不会强行中断 Mutter 内部调用，也不会继续卸载显卡。

## 实现范围

### fd 生命周期插桩版（2026-11-06）

新构建清单标记 `fd_lifecycle_trace=true`；启动器据此在单次试用进程启用 `MUTTER_GPU_RELEASE_POC_TRACE_FDS=1`。
在 MetaEgl 的设备枚举、获取平台显示、initialize/terminate，以及 GBM create/destroy 前后读取 `/proc/self/fd`，仅记录 DRM/NVIDIA 节点。
记录 EGLDisplay、native display/GBM、RenderDevice 类型/路径等关联信息；EGL 返回值只记录原调用结果，不增加额外的 `eglGetError` 调用。
扫描保存并恢复 errno，最多扫描 4096 项，报告截断或不可用。每帧渲染路径不插入扫描。

fd 快照不是原子事务：其他线程可能并发打开、关闭或复用 fd。快照只能缩小初始化阶段，不能证明哪个对象拥有某个 fd，也不能证明驱动内部资源均已销毁。
这些日志覆盖 Mutter wrapper 的边界；Cogl 或驱动内部绕过 wrapper 的 EGL 调用未单独插桩。平台枚举若发生在驱动内部，只能从外层边界的 fd 变化间接观察。
新版本已在独立前缀编译，GI 一致性和开启插桩的隔离 GNOME 启动通过；实际 NVIDIA 会话尚未加载新版。
真实编译有两条未修改 Clutter layout 源码的 maybe-uninitialized 警告；三个 companion 在运行时仍统一使用系统同版本库。

启动期事件不能向现有会话补录。仅在保存工作并手动加载新版试用后运行 Inspect 即可；不要为采集日志执行 Reclaim。

- 默认关闭；只有 Shell 进程带 `MUTTER_GPU_RELEASE_POC=1` 才注册实验接口和日志。
- 会话总线：`org.gnome.Mutter.GpuReleasePoC`，对象 `/org/gnome/Mutter/GpuReleasePoC`，接口 `org.gnome.Mutter.GpuReleasePoC1`。
- `Inspect(s) -> a{sv}` 读取缓存、保留理由、主卡及视图；`Reclaim(s) -> a{sv}` 排队到主线程 idle，重新检查后只删除指定副卡的每卡渲染数据，复用原有析构路径。
- 要求 AMD primary、NVIDIA secondary、活跃 native/logind 会话、AMD 当前 view，以及无目标卡 view / 活跃 CRTC / 已连接 KMS state。存在未完成模式设置、残留 onscreen 或省电 page flip 时拒绝。
- 连接器的 C 侧检查使用 Mutter 已知的 KMS 状态；客户端另外要求管理服务的新鲜 sysfs 状态明确为 disconnected。它不是与热插拔并发完全原子的事务。
- 日志包括正常回收决策、缓存创建/销毁、RenderDevice dispose/finalize、EGL/GBM 清理调用前后、DeviceFile 引用增减及 KMS hold/unhold/reopen。
- DeviceFile/KMS 高频日志只匹配 `MUTTER_GPU_RELEASE_POC_TRACE_DEVICE` 指定节点。启动器尽量按目标 PCI 解析节点；若卡在登录后才出现，使用启用时记录的节点。编号变化时日志可能不完整，应检查过滤节点，不能把未打印当成零引用。
- EGL terminate 日志中的 `returned` 只表示调用返回，未将其包装成驱动内部资源均已释放的证明。
- 不实现持久逻辑离线，不阻止 Mutter 后续重新探测设备，不迁移客户端 GPU 上下文，不暴露任意脚本或任意路径写入接口。

控制入口属于显式 opt-in 的当前用户会话；同一用户的会话程序可调用它，不能用于生产环境的权限边界。

## 构建与重现

私有构建当前位于：`~/.cache/usb4-gpu-manager/mutter-poc/`。
`manifest.json` 包含补丁和库校验和；`configure.log`、`compile.log`、`install.log`、`gnome-shell-linkage.txt` 保留构建证据。

如需从头准备，在 Ubuntu 26.04 amd64、已安装本机运行时和编译器的环境执行：

```bash
/usr/bin/python3 experimental/mutter-poc/bootstrap.py --build
```

脚本校验固定 Ubuntu 源码归档，应用发行版补丁，仅下载构建依赖并解压到用户目录。
不使用 sudo、不 apt install、不修改系统 sources；环境仍依赖本机已安装的工具、运行库和包索引，不是容器化的完全可移植构建。
`sysroot` 已存在时复用，不能在其 host-file symlink 上重复解包；需要不同依赖集时使用新的 `--work`。

也可以使用已经应用 Ubuntu 补丁的干净源码和完整构建依赖：

```bash
/usr/bin/python3 experimental/mutter-poc/build.py --source /path/to/clean/mutter-50.1
```

`gpu-release-poc.patch` 可独立审阅；`gpu-release-poc.sources.json` 校验被修改的原始文件。
`make_patch.py` 从未改动的基线重新生成补丁，不直接编辑系统或传入源码。
默认不构建 GIR；使用本机同版本 typelib，同时将私有目录中的三个未修改 companion SONAME 链接统一指向系统文件。
不能只依赖 `LD_LIBRARY_PATH`：GNOME 的 GI 搜索路径可能直接打开系统目录中的库。
构建现在要求库身份检查和完整的隔离 GNOME JS 启动通过，才写入可试用的清单。完整上游 Mutter 测试套件未在此构建中启用。

独立重查（不启用登录覆盖、不接触真实 GPU）：

```bash
/usr/bin/python3 -I experimental/mutter-poc/preflight.py
/usr/bin/python3 -I experimental/mutter-poc/headless_smoke.py
```

后者需要已安装的 `bubblewrap`、Mesa 软件渲染器、`dbus-run-session` 和 GNOME；缺失时失败退出，不自动安装或降低隔离条件。
沙箱只写临时 HOME，使用独立 D-Bus，不提供 `/dev/dri`、宿主系统总线或宿主会话总线；GNOME 的系统总线代理指向同一个空的私有总线。
私有总线关闭自身的 AppArmor 查询以避免写只读 securityfs，不改宿主 AppArmor 配置。日志中因缺失系统服务出现的提示不能当成物理桌面功能验证通过。

隔离测试：

```bash
/usr/bin/python3 experimental/mutter-poc/test.py
/usr/bin/python3 -m unittest tests.test_poc_control -v
```

C 测试编译实际的接口 `.inc`，使用惰性 GPU 对象与真实独立 D-Bus，启用 AddressSanitizer / UBSan。
不使用真实图形设备，不能据此宣称 GPU 回收实机成功。

## 许可证

生成的 Mutter 补丁、overlay C/header 和 C harness 按 GPL-2.0-or-later 提供，与 Mutter 许可证兼容；许可证文本见 `COPYING.GPL-2`。
其余本项目工具遵循项目 GPL-3.0-only 许可证。此目录不改变上游 Mutter 或本项目其他文件的许可证。


## v5: explicit internal RTX 4070 experiment

`session.py enable --drm-only --internal-4070 --persistent --manifest <v5-manifest>`
opts the validated internal 10de:2820 / 10de:22bd pair into the DRM-only CPU-copy
path. The graphics function must not be a boot GPU, and the IOMMU group must
contain only that graphics/audio pair. AMD must already drive an enabled internal
panel. The default external-GPU policy remains opt-in and still rejects boot GPUs.
The C path separately checks the exact internal PCI address, vendor, product and
non-boot status, and rejects devices marked removable. It does not generalize this
policy to other internal or NVIDIA GPUs.

The internal experiment prevents normal NVIDIA EGL/secondary-context setup and
excludes the selected card from DRM-lease announcements to Xwayland. It retains
the CPU-copy display path and existing primary/view/CRTC/pending-work guards.
This does not force-close KMS or logind references. A remaining KMS hold is still
a blocker; actual desktop unloading and HDMI output need live validation.

A separately verified v5 manifest also enables startup cleanup of LD_LIBRARY_PATH
and restoration of ordinary XDG_DATA_DIRS after the native renderer starts. The
compositor loader has already captured its startup library search paths, while
new child processes receive a clean environment. Already running applications
from the previous desktop session are not repaired by preparing a new session.

After restarting, inspect with:

```sh
/usr/bin/python3 -I experimental/mutter-poc/control.py inspect --internal-4070
```

If the internal GPU is already isolated by boot configuration, first inspect the
manager state. Test loading only with the AMD panel active. Do not infer safe
removal from a cache-reclaimed flag. Re-enable/disable HDMI and confirm rendering,
cursor behavior and holder release in a controlled hardware test.

Recovery from a TTY:

```sh
/usr/bin/python3 -I experimental/mutter-poc/session.py disable
```

The v5 build passed 136 Python tests, 10 ASan/UBSan C tests, library-identity checks,
GPU-free isolated GNOME startup and AMD/internal-NVIDIA render-only startup. These
are not a live GNOME switch or successful internal-GPU unload. See
`docs/reports/evidence/20261113-mutter-internal-v5-build.json` in the project root.
