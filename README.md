# GPU Dock Manager

原生 GTK4 / libadwaita 内置与外置显卡管理程序：查看连接与显示状态，检查安全拔卡条件，部署并迁移外接 NVIDIA GPU 的启动、热插拔和唤醒配置。

**0.1.2 · Alpha**。首版面向 Ubuntu + NVIDIA 开源驱动，从 RTX PRO 5000 Blackwell / AMD 内屏 / 内置独显 VFIO 隔离的实际问题发展而来。
部分功能已在开发者机器上实测，并包含自动测试和 GUI 检查；新的设备、发行版及完整睡眠/热插拔组合尚未验证。

0.1.1 修复驱动清理卡住时状态冻结：USB4 连接独立采集，详细状态超时后标记过期并清空旧指标。
DRM 查询、PCI 移除与绑定改为独立进程，超时不会无限等待无法退出的内核调用，也不会不断启动重复查询。
预约等待、正在移除和完成可拔线分别显示。注销成功不代表移除完成。

0.1.2 将外屏输出和 GPU 移除分开：GNOME 当前用户会话可单独停用 eGPU 外屏、恢复原布局。
显示器线仍连接与桌面输出已停用分别显示。停止输出不会自动终止计算或卸载显卡；桌面仍持有 GPU 时仍需注销后移除。

仓库：https://github.com/Wenbo-Zhong/gpu-dock-manager

项目名称为 GPU Dock Manager。为兼容已有安装，软件包、命令、D-Bus 与服务标识暂时保留 `usb4-gpu-manager`。跨发行版进展见 [移植说明](docs/distribution-support.md)。

## 功能

- **状态总览**：目标 GPU、驱动、显存、温度、功耗、USB4 总速率、休眠参数和恢复事件。
- **显示器归属**：动态映射 PCI 与 DRM，区分内屏、其他 GPU 外屏、eGPU 外屏。物理连接、内核启用状态、GNOME 桌面输出状态分别显示；未知状态拒绝移除。
- **独立外屏操作**：停用 eGPU 上的全部外屏，保留已启用的另一张 GPU 内屏；保存并可恢复原布局。仅修改当前 GNOME 会话的临时显示配置，无需管理员授权，也不要求先迁移 GPU 策略。
- **安全移除**：检查所有 GPU 设备句柄、显示器与启动显卡，通过后只移除目标 GPU 和音频功能。不强卸模块、不杀进程、不移除整个扩展坞桥。
- **注销后移除**：GNOME 持有 DRM 时，临时让下一次会话跳过该卡，等待当前会话释放。可取消，180 秒超时恢复规则。
- **重新连接**：扫描 PCI 总线，等 USB4 路径和 BAR 稳定后加载完整 NVIDIA 模块、绑定设备。
- **可回滚部署**：预览文件、选择目标和可选内置 GPU 隔离，备份旧配置、迁移服务、重建 initramfs。
- **一个自定义后台服务**：`usb4-gpu-manager.service` 处理检测、加载、logind 睡眠事件、唤醒恢复和日志；厂商与系统官方服务仍由发行版管理。
- **权限隔离**：普通用户 GUI，外屏由用户会话管理；GPU 操作经系统 D-Bus / Polkit 校验活动本地会话后由后台服务执行，日常加载/卸载免输密码；部署和恢复系统配置仍需管理员授权。

## 直接预览

在源码目录运行；尚未安装后台服务时自动进入只读模式：

```bash
/usr/bin/python3 -m usb4_gpu_manager
```

使用系统 Python/PyGObject，不使用缺少 GI 的 Conda Python。
界面要求 GTK 4.12+ 与 libadwaita 1.4+；默认使用 CPU 渲染，避免管理器自己占用待移除的 GPU。

## 安装与迁移

已有 GTK4/libadwaita 的 Ubuntu：

```bash
sudo bash install.sh
usb4-gpu-manager
```

重装 Linux 后，先通过发行版安装兼容的 NVIDIA 开源驱动，再执行：

```bash
sudo bash install.sh --install-deps
usb4-gpu-manager
```

安装程序安装应用、D-Bus/Polkit 定义和一个只读起步的后台服务，**不会立即替换现有 GPU 策略**。
在“部署与恢复”页面选择外接显卡、按需勾选内置独显 VFIO 隔离，点击“预览部署变更”。确认具体文件后授权部署。
备份保存在 `/var/lib/usb4-gpu-manager/backups/`，重启后启用新策略。

没有可移除目标显卡、驱动不支持内核 PM 通知器、待隔离设备正在驱动显示器、同型号显卡无法唯一识别时拒绝部署。
首版自动部署要求 initramfs-tools；其他发行版可以尝试只读界面，暂不自动配置。
应用保留 NVIDIA 显示模块及 modeset=1，启用 `NVreg_UseKernelSuspendNotifiers=1`。
显示器检测用于展示、日志和拔卡检查；不会因未接屏而强制卸载显卡。

## 安全带走笔记本

1. 保存工作，结束 GPU 计算，并打开由 AMD/Intel 等另一张显卡驱动的内屏。
2. 接有 eGPU 外屏时，可以先点“停用外屏”，确认仅在内屏工作。屏幕和显卡是独立操作；只想停用外屏继续计算时，到此即可。
3. 若“安全移除”可用，点击后等待“可以拔线”。显示器线可以留在显卡上，但后台必须确认所有外屏输出已关闭、另一张 GPU 的内屏已启用且进程占用已释放。
4. GNOME 占用或 eGPU 仍连接外屏时，选择“注销后移除”，确认后执行系统注销。外屏会停止输出，显示器线可以留在显卡上。
5. 通过内屏重新登录、打开管理器，确认已完成安全移除，再拔 USB4 线。

“停用外屏”通过 GNOME Mutter `ApplyMonitorsConfig` 先校验、再临时应用，不写入永久显示设置。
接口依据 [Mutter 50.1 显示配置定义](https://github.com/GNOME/mutter/blob/50.1/data/dbus-interfaces/org.gnome.Mutter.DisplayConfig.xml)。
需事先在 GNOME 显示设置中启用另一张 GPU 的内屏；不允许关闭唯一的可用显示器。
停用后其余逻辑屏幕临时横向排列，保留模式、缩放、旋转、镜像组和颜色设置。
“恢复外屏”仅在同一张显卡、相同屏幕身份且当前布局仍为本程序停用后的布局时可用，防止覆盖后续人工调整。
布局备份属于当前用户，位于 `${XDG_STATE_HOME:-~/.local/state}/usb4-gpu-manager/display-layout.json`，权限 0600。
注销后临时布局可能被桌面重置；布局/拓扑变化时请用 GNOME 显示设置重新启用外屏。当前仅支持 GNOME 显示配置接口。
物理线缆已连接仍按接屏显示；息屏造成的内核输出关闭不当作桌面停用外屏。

预约要求另一张 GPU 的内屏已启用；执行移除前会再次验证。未事先确认的新显示器接入、状态未知、GPU 身份变化或占用未释放会阻止移除。
取消/超时恢复正常接管规则；已经跳过 GPU 的 GNOME 会话可能需重新登录才能使用其外屏。
管理器不终止进程；用户确认的正常注销会结束桌面会话。
若拔线后 NVIDIA 报 Xid 79、PCI 条目残留且驱动持续阻塞，界面显示异常断开并暂停设备操作。
软件不能解除内核中的不可中断等待，请保存工作、保持显卡断开并正常重启。
旧版本已经卡住时应先重启再运行更新安装脚本，避免反复重启服务。

重新接回显卡时会检测 USB4 设备重新出现并延迟加载，也可手动“重新连接”。若缺少 USB4 路由器信息，移除后自动重连保持暂停，请手动连接。

## 命令行与恢复

状态总览并列显示内置与外置显卡的状态、显示器和操作。内置 RTX 4070 支持实验性的「加载」与「卸载」。目前仅支持图形 `10de:2820`、音频 `10de:22bd` 且同一 IOMMU 组仅有这两个功能的机器。加载将两项功能绑定到 `nvidia` 和 `snd_hda_intel`，卸载将两项功能切换到 `vfio-pci`。切换仅影响本次开机，不改开机规则。

启用 AMD 内屏并释放相关 GPU、音频和 VFIO 占用后，可以先用 4070 区域的「停用外屏」关闭其输出，再手动卸载。两张卡分别保存显示布局，恢复仍要求显示器身份和当前布局匹配。共享 NVIDIA 设备节点也可能阻止切换。发生普通绑定错误时会尝试回退。内核调用超时后先查看实际状态，必要时重启恢复开机配置。

支持的 4070 机器会在服务启动时记录电源和 USB4 状态，之后每个确认的状态变化只尝试一次。接入外置卡时尝试加载外置卡，并在 4070 空闲且未接屏时隔离它。用电池时也尝试隔离空闲且未接屏的 4070。接电且无外置卡时尝试加载 4070。占用阻止切换后，同一状态下由用户手动操作。手动安全移除外置卡后，USB4 仍接着时不会被后台立即重新加载。

笔记本 HDMI 由 4070 驱动。自动策略会保护已检测到的连接，即使该输出已被手动停用。4070 绑定 VFIO 后，系统无法通过它的 DRM 接口感知新插入的 HDMI 线，此时先手动加载 4070。

```bash
usb4-gpu-managerctl internal-detach --pci 0000:65:00.0
usb4-gpu-managerctl internal-attach --pci 0000:65:00.0
usb4-gpu-managerctl internal-status --pci 0000:65:00.0
usb4-gpu-managerctl internal-disable-displays --pci 0000:65:00.0
usb4-gpu-managerctl internal-restore-displays --pci 0000:65:00.0
```

4070 面板的「检查占用」只读取当前图形、HDMI 音频驱动及持有进程。「加载」和「卸载」也先展示这项检查，之后才允许确认切换。属于本设备的节点和共享 NVIDIA 节点分别标注，实际切换前会再次检查。

手动卸载时，`nvidia-powerd` 和仅持有声卡控制节点的 `wireplumber` 会标为「自动释放」。客户端按需暂停当前用户的 WirePlumber，特权工作进程暂停正在运行的 nvidia-powerd，然后重新执行完整占用与显示输出检查。其他应用或仍未释放的句柄会阻止切换。WirePlumber 在操作完成、失败或授权取消后恢复，这段时间其他声音也可能短暂中断。nvidia-powerd 在卸载失败时恢复；成功卸载后保留暂停标记，之后由本程序加载 4070 时恢复。原本未运行的服务不会因此启动，不修改服务启用设置。后台自动策略不会暂停用户的 WirePlumber，无法释放时仍只尝试一次。

若客户端或系统异常中断导致服务未恢复，可手动执行 `systemctl --user start wireplumber.service`；确认 4070 已加载后可执行 `sudo systemctl start nvidia-powerd.service`。这部分流程尚需在真实卸载和重新加载场景中验证。

以下为外置卡和系统恢复操作。

```bash
usb4-gpu-managerctl status
usb4-gpu-managerctl logs
usb4-gpu-managerctl attach
usb4-gpu-managerctl detach
usb4-gpu-managerctl cancel-detach
usb4-gpu-managerctl disable-displays
usb4-gpu-managerctl restore-displays
```

正常回滚：界面“恢复部署前配置”，或 `usb4-gpu-managerctl rollback`，然后重启。
后台无法运行时，在 TTY 中直接恢复最近备份：

```bash
sudo usb4-gpu-managerctl recover
```

若 initramfs 重建失败，检查 /boot 空间后执行 `sudo update-initramfs -u -k all`。
恢复、重启验证后，可运行 `sudo bash uninstall.sh` 卸载应用；部署备份仍保留。

## 开发与验证

TTY 中检查 GNOME 是否释放 GPU 的独立实验脚本见 [tools/README.md](tools/README.md)。
该 demo 只记录占用并自动返回原桌面，不执行显卡卸载；无需重新安装服务。
源码研究、实测证据与后续 PoC 方案见 [Mutter GPU 资源释放研究报告（2026-11-05）](docs/reports/2026-11-05-mutter-gpu-release.md)。
引用日志与副卡缓存回收实验见 [Mutter PoC](experimental/mutter-poc/README.md)。初版登录故障已修复，新增隔离启动检查和单次试用保护；实机实验会话现已成功登录。副卡缓存自动销毁后仍有 NVIDIA 句柄，引用释放及安全移除尚未实现。该实验不随应用安装替换系统桌面。
当前已构建独立的 [DRM-only／CPU-copy 实验版](docs/reports/2026-11-09-drm-only-poc.md)，通过编译和隔离启动；真实外屏输出与安全移除尚未验证。

```bash
/usr/bin/python3 -m unittest discover -v
/usr/bin/dbus-run-session -- /usr/bin/python3 -m tests.dbus_smoke
/usr/bin/python3 -m compileall -q usb4_gpu_manager data
bash -n install.sh uninstall.sh
systemd-analyze verify data/usb4-gpu-manager.service
desktop-file-validate data/io.github.usb4gpumanager.App.desktop
```

测试中的 sysfs 写入、配置部署、initramfs 操作使用临时目录或 mock。D-Bus 测试使用独立总线和模拟控制器，不接管真实系统总线、不移除真实显卡。

架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，权限边界见 [SECURITY.md](SECURITY.md)，贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)，主程序许可证为 [GPL-3.0-only](LICENSE)，Mutter 补丁保留 GPL-2.0-or-later，见 [第三方声明](NOTICE.md)。
顶层旧的 install-kernel-pm.sh、egpu-display-status.py 等为之前修复实验保留，新安装程序不重复安装这些脚本。research/ 与本机截图不属于发行内容。

参考：[NVIDIA 595.99.02 电源管理说明](https://download.nvidia.com/XFree86/Linux-x86_64/595.99.02/README/powermanagement.html)。

## 原生软件包与 Mutter 补丁

应用可通过 `make deb` 或 `make rpm` 构建，由 APT 或 DNF 安装。发行版安装包同时包含 Mutter 补丁、源码指纹、移植 skill 和可选的用户级更新提醒。[打包与旧安装迁移](docs/packaging.md) · [Mutter 移植与系统安装](docs/mutter-porting.md)

## 托盘与后台权限

桌面支持 StatusNotifierItem 时，最小化或关闭窗口会将界面隐藏到托盘，标题栏的退出按钮用于彻底退出界面。托盘菜单提供显示、隐藏和退出；再次打开快捷方式会恢复已有窗口。托盘不可用时不隐藏到不可访问的后台；托盘宿主退出时会恢复窗口。退出界面不停止系统管理服务。正在执行设备操作或等待注销时需先完成或取消操作再退出界面。

托盘通过会话 D-Bus 实现，不额外依赖 GTK3 AppIndicator。GNOME 需要启用支持 AppIndicator/StatusNotifierItem 的扩展，KDE 等桌面通常内置托盘宿主。用户界面保持普通用户权限；活动本地会话的固定 GPU 操作由 root 后台服务执行，非活动及远程会话默认拒绝。系统部署与恢复继续要求管理员授权。此策略适用于 GUI 和命令行客户端。
